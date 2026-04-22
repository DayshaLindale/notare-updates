"""Notare Update Server — Standalone deployment for serving client updates.

Deploy this to any cloud host (Render, Railway, Fly.io, VPS, etc.).
Manages update packages and serves them to Notare desktop clients.

Usage:
    python server.py                    # Run locally on port 9000
    gunicorn server:app -b 0.0.0.0:$PORT  # Production (Render, etc.)

Environment variables:
    ADMIN_KEY   — Required. Secret key for uploading updates (set in your host's env vars)
    PORT        — Server port (default 9000, most hosts set this automatically)
"""

import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Header, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
UPDATES_DIR = BASE_DIR / "packages"
MANIFEST_FILE = BASE_DIR / "manifest.json"
UPDATES_DIR.mkdir(exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
if not ADMIN_KEY:
    import secrets
    ADMIN_KEY = secrets.token_hex(32)
    print(f"WARNING: No ADMIN_KEY set in environment. Generated temporary: {ADMIN_KEY[:16]}...")
    print("Set ADMIN_KEY in your hosting environment variables for persistence.")

app = FastAPI(title="Notare Update Server", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Static site files (served at /static/ and root)
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Manifest — tracks current version and update history
# ---------------------------------------------------------------------------

def load_manifest():
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return {"current_version": "0.0.0", "updates": []}


def save_manifest(m):
    MANIFEST_FILE.write_text(json.dumps(m, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Client endpoints — called by Notare desktop app on startup
# ---------------------------------------------------------------------------

@app.get("/api/manifest")
async def get_manifest():
    """Return the full manifest — used by in-app banner to check version."""
    return JSONResponse(load_manifest())


@app.get("/api/update")
async def check_update(current: str = "0.0.0"):
    """Client calls this to check if a newer version exists."""
    manifest = load_manifest()
    server_version = manifest.get("current_version", "0.0.0")

    if current < server_version:
        pkg = UPDATES_DIR / f"notare-{server_version}.zip"
        if pkg.exists():
            return JSONResponse({
                "update_available": True,
                "version": server_version,
                "url": f"/api/update/download/{server_version}",
                "size": pkg.stat().st_size,
                "released": manifest.get("released", ""),
            })
        else:
            # Patch hosted on GitHub Releases (small, fast download)
            return JSONResponse({
                "update_available": True,
                "version": server_version,
                "url": f"https://github.com/DayshaLindale/notare-updates/releases/download/v{server_version}/notare-{server_version}-patch.zip",
                "released": manifest.get("released", ""),
                "patch": True,
            })

    return JSONResponse({"update_available": False, "version": server_version})


@app.get("/api/update/download/{version}")
async def download_update(version: str):
    """Serve the update zip for a specific version."""
    pkg = UPDATES_DIR / f"notare-{version}.zip"
    if pkg.exists():
        return FileResponse(str(pkg), filename=f"notare-{version}.zip",
                            media_type="application/zip")
    return JSONResponse({"error": "Package not found"}, status_code=404)


@app.get("/api/validate-key")
async def validate_key(key: str = ""):
    """Server-side license validation. Fallback when local admin_data doesn't have the key."""
    if not key:
        return JSONResponse({"valid": False, "reason": "no_key"})

    # Load admin data from the server's copy
    admin_file = BASE_DIR / "admin_data.json"
    if not admin_file.exists():
        return JSONResponse({"valid": False, "reason": "no_database"})

    try:
        admin_data = json.loads(admin_file.read_text(encoding="utf-8"))
        for lic in admin_data.get("licenses", []):
            if lic.get("key", "").upper() == key.upper() and lic.get("status") == "active":
                exp = lic.get("expires", "")
                if exp and exp != "":
                    from datetime import datetime
                    if exp < datetime.now().strftime("%Y-%m-%d"):
                        return JSONResponse({"valid": False, "reason": "expired"})
                return JSONResponse({
                    "valid": True,
                    "tier": lic.get("tier", "solo"),
                    "expires": lic.get("expires", ""),
                    "customer": lic.get("customer_name", ""),
                })
    except Exception:
        pass

    return JSONResponse({"valid": False, "reason": "invalid_key"})


@app.post("/api/validate-license")
async def validate_license_post(request: Request):
    """POST-based license validation with hardware binding.

    First activation: server records machine_id. Subsequent validations must
    match. Key works on one machine only. Deactivation releases the binding.
    """
    try:
        data = await request.json()
        key = data.get("key", "")
        machine_id = data.get("machine_id", "")  # Client sends hashed hardware ID
    except Exception:
        return JSONResponse({"valid": False, "reason": "bad_request"})
    if not key:
        return JSONResponse({"valid": False, "reason": "no_key"})

    admin_file = BASE_DIR / "admin_data.json"
    if not admin_file.exists():
        return JSONResponse({"valid": False, "reason": "no_database"})

    try:
        admin_data = json.loads(admin_file.read_text(encoding="utf-8"))
        save_needed = False

        for lic in admin_data.get("licenses", []):
            if lic.get("key", "").upper() != key.upper():
                continue
            if lic.get("status") != "active":
                return JSONResponse({"valid": False, "reason": "inactive"})

            # Check expiration
            exp = lic.get("expires", "")
            if exp and exp != "":
                if exp < datetime.now().strftime("%Y-%m-%d"):
                    return JSONResponse({"valid": False, "reason": "expired"})

            # Hardware binding
            bound_machine = lic.get("machine_id", "")
            if machine_id:
                if not bound_machine:
                    # First activation — bind to this machine
                    lic["machine_id"] = machine_id
                    lic["activated_at"] = datetime.now().isoformat()
                    save_needed = True
                elif bound_machine != machine_id:
                    # Already bound to a different machine
                    return JSONResponse({
                        "valid": False,
                        "reason": "wrong_machine",
                        "message": "This key is activated on another computer. Contact support to transfer.",
                    })

            if save_needed:
                admin_file.write_text(json.dumps(admin_data, indent=2), encoding="utf-8")

            return JSONResponse({
                "valid": True,
                "tier": lic.get("tier", "solo"),
                "expires": lic.get("expires", ""),
                "customer": lic.get("customer_name", ""),
                "bound": bool(lic.get("machine_id")),
            })
    except Exception:
        pass

    return JSONResponse({"valid": False, "reason": "invalid_key"})


@app.post("/api/license/deactivate")
async def deactivate_license(request: Request):
    """Admin: release a license's machine binding for transfer to new computer."""
    auth = request.headers.get("authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    key = data.get("key", "").upper()
    if not key:
        return JSONResponse({"error": "No key provided"}, status_code=400)

    admin_file = BASE_DIR / "admin_data.json"
    try:
        admin_data = json.loads(admin_file.read_text(encoding="utf-8"))
        for lic in admin_data.get("licenses", []):
            if lic.get("key", "").upper() == key:
                old_machine = lic.pop("machine_id", None)
                lic.pop("activated_at", None)
                admin_file.write_text(json.dumps(admin_data, indent=2), encoding="utf-8")
                return JSONResponse({
                    "ok": True,
                    "message": f"Binding released for {lic.get('customer_name', key[:8])}",
                    "previous_machine": old_machine,
                })
        return JSONResponse({"error": "Key not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Support ticket relay
# ---------------------------------------------------------------------------

_pending_tickets = []

@app.post("/api/support/submit")
async def submit_support_ticket(request: Request):
    """Receive a support ticket from a client installation."""
    try:
        data = await request.json()
        _pending_tickets.append(data)
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

@app.get("/api/support/pending")
async def get_pending_tickets(request: Request):
    """Support users poll this for new tickets."""
    return JSONResponse({"tickets": _pending_tickets})

@app.post("/api/support/claim")
async def claim_ticket(request: Request):
    """Mark a ticket as claimed."""
    try:
        data = await request.json()
        ticket_id = data.get("ticket_id")
        _pending_tickets[:] = [t for t in _pending_tickets if t.get("id") != ticket_id]
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

@app.post("/api/notify")
async def receive_notification(request: Request):
    """Receive contact form and help request notifications."""
    try:
        data = await request.json()
        _pending_tickets.append(data)
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

@app.get("/api/notifications")
async def get_notifications(request: Request):
    """Support users poll for notifications."""
    return JSONResponse({"notifications": _pending_tickets})


@app.post("/api/admin/sync")
async def sync_admin_data(request: Request):
    """Receive synced admin_data from the master installation."""
    auth = request.headers.get("Authorization", "")
    if auth != "notare-admin-sync":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        data = await request.json()
        admin_file = BASE_DIR / "admin_data.json"
        admin_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "licenses": len(data.get("licenses", []))})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Share Code Relay — Pure transport, nothing persisted. Data exists in RAM
# only while actively needed, then is burned immediately.
# ---------------------------------------------------------------------------

_shared_transcripts = {}  # code -> payload (RAM only, never written to disk)

@app.post("/api/share/relay")
async def share_relay(request: Request):
    """Reporter pushes a shared transcript to the server with a short code."""
    auth = request.headers.get("Authorization", "")
    if auth != "notare-admin-sync":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        data = await request.json()
        code = data.get("code", "")
        if not code:
            return JSONResponse({"error": "No code"}, status_code=400)
        data["_pull_count"] = 0  # track if recipient has pulled
        _shared_transcripts[code] = data
        return JSONResponse({"ok": True, "code": code})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/share/relay-update")
async def share_relay_update(request: Request):
    """Reporter pushes updated segments for a live share."""
    auth = request.headers.get("Authorization", "")
    if auth != "notare-admin-sync":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        data = await request.json()
        code = data.get("code", "")
        if code in _shared_transcripts:
            _shared_transcripts[code]["segments"] = data.get("segments", [])
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Code not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/share/burn/{code}")
async def share_burn(code: str, request: Request):
    """Reporter explicitly burns a share code. Data is deleted immediately."""
    auth = request.headers.get("Authorization", "")
    if auth != "notare-admin-sync":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if code in _shared_transcripts:
        del _shared_transcripts[code]
        return JSONResponse({"ok": True, "burned": True})
    return JSONResponse({"ok": True, "burned": False})


@app.get("/api/share/pull/{code}")
async def share_pull(code: str):
    """Recipient pulls a shared transcript by entering the code."""
    payload = _shared_transcripts.get(code)
    if not payload:
        return JSONResponse({"error": "Code not found or burned"}, status_code=404)

    is_live = payload.get("live", False)

    # For non-live (snapshot) shares: burn after first pull
    # The recipient gets it once, then it's gone
    if not is_live:
        payload["_pull_count"] = payload.get("_pull_count", 0) + 1
        if payload["_pull_count"] > 1:
            # Already pulled once — burn it
            del _shared_transcripts[code]
            return JSONResponse({"error": "This share code has already been used"}, status_code=410)

    # Return transcript data (strip internal fields)
    return JSONResponse({
        "code": code,
        "case_caption": payload.get("case_caption", ""),
        "case_number": payload.get("case_number", ""),
        "court_name": payload.get("court_name", ""),
        "date": payload.get("date", ""),
        "proceeding_type": payload.get("proceeding_type", ""),
        "reporter_name": payload.get("reporter_name", ""),
        "speakers": payload.get("speakers", []),
        "segments": payload.get("segments", []),
        "permissions": payload.get("permissions", ["view"]),
        "live": is_live,
    })


# ---------------------------------------------------------------------------
# Proof Config Delivery — encrypted configs served per license key
# Configs are stored server-side in proof_configs/{config_key}_{key_hash}.npc
# Client pulls only configs encrypted for their key
# ---------------------------------------------------------------------------

PROOF_CONFIG_DIR = BASE_DIR / "proof_configs"
PROOF_CONFIG_DIR.mkdir(exist_ok=True)


@app.post("/api/proof-configs/push")
async def push_proof_config(request: Request):
    """Admin pushes an encrypted proof config for a specific license key.

    Body: {
        "config_key": "depo_direct",
        "license_key": "TARGET_KEY",
        "encrypted_data": "base64-encoded encrypted config blob"
    }
    """
    auth = request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    config_key = data.get("config_key", "")
    license_key = data.get("license_key", "")
    encrypted_data = data.get("encrypted_data", "")

    if not config_key or not license_key or not encrypted_data:
        return JSONResponse({"error": "config_key, license_key, and encrypted_data required"}, status_code=400)

    # Store with key hash so we can look up by key without exposing it
    key_hash = hashlib.sha256(license_key.upper().encode()).hexdigest()[:16]
    filename = f"{config_key}_{key_hash}.npc"
    (PROOF_CONFIG_DIR / filename).write_text(encrypted_data, encoding="utf-8")

    return JSONResponse({
        "ok": True,
        "config_key": config_key,
        "key_hash": key_hash,
        "filename": filename,
    })


@app.get("/api/proof-configs/pull")
async def pull_proof_configs(key: str = ""):
    """Client pulls all proof configs assigned to their license key.

    Returns list of {config_key, encrypted_data} — client decrypts locally.
    Server never sees the decryption key (it's the license key itself).
    """
    if not key:
        return JSONResponse({"configs": []})

    key_hash = hashlib.sha256(key.upper().encode()).hexdigest()[:16]
    configs = []

    for f in PROOF_CONFIG_DIR.glob(f"*_{key_hash}.npc"):
        config_key = f.stem.rsplit("_", 1)[0]
        encrypted_data = f.read_text(encoding="utf-8")
        configs.append({
            "config_key": config_key,
            "encrypted_data": encrypted_data,
        })

    return JSONResponse({"configs": configs, "count": len(configs)})


@app.delete("/api/proof-configs/{config_key}/{key_hash}")
async def delete_proof_config(config_key: str, key_hash: str, request: Request):
    """Admin removes a proof config assignment."""
    auth = request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    filename = f"{config_key}_{key_hash}.npc"
    target = PROOF_CONFIG_DIR / filename
    if target.exists():
        target.unlink()
        return JSONResponse({"ok": True, "deleted": filename})
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.get("/api/proof-configs/list-all")
async def list_all_proof_configs(request: Request):
    """Admin lists all proof config assignments."""
    auth = request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    configs = []
    for f in sorted(PROOF_CONFIG_DIR.glob("*.npc")):
        parts = f.stem.rsplit("_", 1)
        configs.append({
            "config_key": parts[0] if len(parts) > 1 else f.stem,
            "key_hash": parts[1] if len(parts) > 1 else "",
            "filename": f.name,
            "size": f.stat().st_size,
        })
    return JSONResponse({"configs": configs, "total": len(configs)})


@app.get("/api/download-installer")
async def download_installer():
    """Redirect to the installer download. User sees notarelegal.com URL, gets the file from GitHub."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        "https://github.com/DayshaLindale/notare-updates/releases/download/v1.2.0/NotareSetup_v1.2.0.exe",
        # v1.2.0: Complete audio fix — assemblyai bundled, local whisper, engine failover, error handling
        status_code=302,
    )


@app.get("/api/update/info")
async def update_info():
    """Public version info — no auth needed."""
    manifest = load_manifest()
    return JSONResponse({
        "current_version": manifest.get("current_version", "0.0.0"),
        "released": manifest.get("released", ""),
        "total_updates": len(manifest.get("updates", [])),
    })


# ---------------------------------------------------------------------------
# Admin endpoints — for pushing new updates (protected by ADMIN_KEY)
# ---------------------------------------------------------------------------

def _check_admin_key(authorization: str = None):
    if not authorization:
        return False
    return authorization == ADMIN_KEY or authorization == f"Bearer {ADMIN_KEY}"


@app.post("/api/update/push")
async def push_update(
    version: str,
    file: UploadFile = File(...),
    authorization: str = Header(None),
):
    """Upload a new update package and set it as the current version.

    Usage:
        curl -X POST "https://your-server/api/update/push?version=0.2.2" \
             -H "Authorization: Bearer YOUR_ADMIN_KEY" \
             -F "file=@updates/notare-0.2.2.zip"
    """
    if not _check_admin_key(authorization):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Save the zip
    dest = UPDATES_DIR / f"notare-{version}.zip"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Update manifest
    manifest = load_manifest()
    manifest["current_version"] = version
    manifest["released"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    manifest.setdefault("updates", []).append({
        "version": version,
        "released": manifest["released"],
        "size": dest.stat().st_size,
        "checksum": hashlib.sha256(dest.read_bytes()).hexdigest(),
    })
    save_manifest(manifest)

    return JSONResponse({
        "ok": True,
        "version": version,
        "size": dest.stat().st_size,
        "message": f"v{version} is now live. All clients will pull this on next launch.",
    })


@app.get("/api/update/history")
async def update_history(authorization: str = Header(None)):
    """View all pushed updates."""
    if not _check_admin_key(authorization):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    manifest = load_manifest()
    return JSONResponse(manifest)


@app.delete("/api/update/{version}")
async def delete_update(version: str, authorization: str = Header(None)):
    """Remove an update package (doesn't rollback clients already updated)."""
    if not _check_admin_key(authorization):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pkg = UPDATES_DIR / f"notare-{version}.zip"
    if pkg.exists():
        pkg.unlink()

    manifest = load_manifest()
    manifest["updates"] = [u for u in manifest.get("updates", []) if u["version"] != version]

    # If we deleted the current version, roll back to the most recent remaining
    if manifest.get("current_version") == version:
        if manifest["updates"]:
            manifest["current_version"] = manifest["updates"][-1]["version"]
        else:
            manifest["current_version"] = "0.0.0"
    save_manifest(manifest)

    return JSONResponse({"ok": True, "message": f"v{version} removed"})


# ---------------------------------------------------------------------------
# Health check (most cloud hosts ping this)
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    """Serve the marketing site if static files exist, otherwise API info."""
    site_file = STATIC_DIR / "site.html"
    if site_file.exists():
        return FileResponse(str(site_file), media_type="text/html")
    manifest = load_manifest()
    return JSONResponse({
        "service": "Notare Update Server",
        "current_version": manifest.get("current_version", "0.0.0"),
        "status": "running",
    })


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Fleet Health Telemetry — clients report issues, we see patterns
# ---------------------------------------------------------------------------

HEALTH_DIR = Path(os.environ.get("HEALTH_DIR", "health_reports"))
HEALTH_DIR.mkdir(exist_ok=True)

@app.post("/api/telemetry/health")
async def receive_health_report(request: Request):
    """Receive health digest from a client. Rate-limited, size-limited, validated."""
    try:
        # Rate limit: max 10KB per report, reject oversized
        body = await request.body()
        if len(body) > 10240:
            return JSONResponse({"ok": False, "error": "report too large"}, status_code=413)
        report = json.loads(body)

        # Validate structure — must have profile and event_types
        if not isinstance(report.get("profile"), dict) or not isinstance(report.get("event_types"), dict):
            return JSONResponse({"ok": False, "error": "invalid format"}, status_code=400)

        machine = report.get("profile", {}).get("machine", "unknown")
        # Rate limit: max 1 report per machine per hour
        existing = list(HEALTH_DIR.glob(f"{machine}_*.json"))
        if existing:
            latest = max(existing, key=lambda p: p.stat().st_mtime)
            age = time.time() - latest.stat().st_mtime
            if age < 3600:
                return JSONResponse({"ok": True, "throttled": True})
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save report
        report_path = HEALTH_DIR / f"{machine}_{timestamp}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        # Check for fleet-wide patterns — if multiple machines report the same error
        recent_reports = sorted(HEALTH_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)[-50:]
        error_types = {}
        for rp in recent_reports:
            try:
                r = json.loads(rp.read_text(encoding='utf-8'))
                for etype, count in r.get("event_types", {}).items():
                    error_types[etype] = error_types.get(etype, 0) + count
            except:
                pass

        # Flag patterns affecting multiple machines
        fleet_issues = {k: v for k, v in error_types.items() if v >= 3}

        return JSONResponse({
            "ok": True,
            "fleet_issues": fleet_issues if fleet_issues else None,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.get("/api/telemetry/fleet")
async def fleet_health(authorization: str = Header(None)):
    """Admin: see fleet-wide health patterns."""
    if not _check_admin_key(authorization):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    reports = sorted(HEALTH_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)[-100:]
    machines = set()
    all_errors = {}
    total_fixes = 0
    total_failed = 0

    for rp in reports:
        try:
            r = json.loads(rp.read_text(encoding='utf-8'))
            machines.add(r.get("profile", {}).get("machine", "?"))
            for etype, count in r.get("event_types", {}).items():
                all_errors[etype] = all_errors.get(etype, 0) + count
            total_fixes += r.get("fixes_applied", 0)
            total_failed += r.get("fixes_failed", 0)
        except:
            pass

    return JSONResponse({
        "machines": len(machines),
        "reports": len(reports),
        "error_types": all_errors,
        "auto_fixes": total_fixes,
        "unresolved": total_failed,
        "fix_rate": round(total_fixes / max(total_fixes + total_failed, 1) * 100, 1),
    })


# ---------------------------------------------------------------------------
# Support ticket relay (clients push tickets here, support users poll here)
# ---------------------------------------------------------------------------

_pending_tickets = []

@app.post("/api/support/submit")
async def submit_support_ticket(request: Request):
    """Receive a support ticket from a client installation."""
    try:
        data = await request.json()
        _pending_tickets.append(data)
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

@app.get("/api/support/pending")
async def get_pending_tickets(request: Request):
    """Support users poll this for new tickets."""
    return JSONResponse({"tickets": _pending_tickets})

@app.post("/api/support/claim")
async def claim_ticket(request: Request):
    """Mark a ticket as claimed (remove from pending)."""
    try:
        data = await request.json()
        ticket_id = data.get("ticket_id")
        _pending_tickets[:] = [t for t in _pending_tickets if t.get("id") != ticket_id]
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

@app.post("/api/notify")
async def receive_notification(request: Request):
    """Receive contact form / help request notifications from client apps."""
    try:
        data = await request.json()
        _pending_tickets.append(data)
        return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

@app.get("/api/notifications")
async def get_notifications(request: Request):
    """Support users poll for notifications."""
    return JSONResponse({"notifications": _pending_tickets})


# ---------------------------------------------------------------------------
# Sozawen — license key generation & validation
# ---------------------------------------------------------------------------
# Keys are deterministic: HMAC-SHA256(session_id, SOZAWEN_KEY_SECRET) → first 12
# base32 chars → SOZA-XXXX-XXXX-XXXX. Same Stripe session always yields the same
# key, so no persistent ledger is required (validation re-derives by listing
# recent completed sessions and recomputing keys until a match is found).
#
# Stripe restricted key is used to read sessions. Stored on Render as
# STRIPE_READ_KEY (restricted to Checkout Sessions:read, Events:read).
# SOZAWEN_KEY_SECRET must match between server and any client that derives keys;
# rotating it invalidates all previously issued keys, so don't.

import hmac as _hmac
import hashlib as _hashlib
import base64 as _base64
import urllib.request as _ureq
import urllib.parse as _uparse
import urllib.error as _uerr

SOZAWEN_KEY_SECRET = os.environ.get("SOZAWEN_KEY_SECRET", "")
STRIPE_READ_KEY = os.environ.get("STRIPE_READ_KEY", "")


def _sozawen_derive_key(session_id: str) -> str:
    """Deterministic license key for a Stripe checkout session."""
    if not SOZAWEN_KEY_SECRET:
        raise RuntimeError("SOZAWEN_KEY_SECRET not configured")
    mac = _hmac.new(SOZAWEN_KEY_SECRET.encode(), session_id.encode(), _hashlib.sha256).digest()
    b32 = _base64.b32encode(mac).decode().rstrip("=")[:12]
    return f"SOZA-{b32[0:4]}-{b32[4:8]}-{b32[8:12]}"


def _stripe_get(path: str, params: dict | None = None) -> dict:
    """Minimal Stripe GET helper using restricted read key."""
    if not STRIPE_READ_KEY:
        raise RuntimeError("STRIPE_READ_KEY not configured")
    url = f"https://api.stripe.com{path}"
    if params:
        url += "?" + _uparse.urlencode(params)
    auth = _base64.b64encode(f"{STRIPE_READ_KEY}:".encode()).decode()
    req = _ureq.Request(url, headers={"Authorization": f"Basic {auth}"})
    with _ureq.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


@app.get("/api/sozawen/key")
async def sozawen_key_for_session(session_id: str):
    """Return the license key for a completed Stripe checkout session.

    Called by /thank-you.html after Stripe redirects. Verifies the session is
    real and paid before computing the key so the endpoint can't be used to
    fish keys for unpaid or fabricated session IDs.
    """
    if not session_id or not session_id.startswith(("cs_live_", "cs_test_")):
        return JSONResponse({"error": "invalid session_id"}, status_code=400)
    try:
        session = _stripe_get(f"/v1/checkout/sessions/{session_id}")
    except _uerr.HTTPError as e:
        return JSONResponse({"error": f"stripe: {e.code}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if session.get("payment_status") != "paid":
        return JSONResponse({"error": "session not paid"}, status_code=402)

    try:
        key = _sozawen_derive_key(session_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    meta = session.get("metadata") or {}
    return JSONResponse({
        "key": key,
        "email": session.get("customer_details", {}).get("email") or session.get("customer_email"),
        "type": meta.get("type", "self_purchase"),
        "pool": meta.get("pool", "revenue"),
        "amount_total": session.get("amount_total"),
        "currency": session.get("currency"),
    })


@app.post("/api/sozawen/validate")
async def sozawen_validate_key(request: Request):
    """Validate a Sozawen license key.

    Strategy: list recent completed Stripe sessions, recompute each key, return
    valid if any matches. Cached in memory for 10 minutes to avoid hammering
    Stripe. Good enough for launch scale; switch to a proper ledger past a few
    thousand customers.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"valid": False, "message": "bad request"}, status_code=400)

    key = str(body.get("key", "")).strip().upper()
    if not key.startswith("SOZA-") or len(key) != 19:
        return JSONResponse({"valid": False, "message": "Invalid key format"})

    # Cached session listing (10-minute TTL)
    now = time.time()
    cache = getattr(app.state, "sozawen_sessions", None)
    cache_time = getattr(app.state, "sozawen_sessions_at", 0)
    if cache is None or now - cache_time > 600:
        sessions = []
        starting_after = None
        # Walk up to ~10 pages (1000 sessions) back
        for _ in range(10):
            params = {"limit": "100", "status": "complete"}
            if starting_after:
                params["starting_after"] = starting_after
            try:
                page = _stripe_get("/v1/checkout/sessions", params)
            except Exception:
                break
            page_data = page.get("data", [])
            sessions.extend(page_data)
            if not page.get("has_more") or not page_data:
                break
            starting_after = page_data[-1]["id"]
        app.state.sozawen_sessions = sessions
        app.state.sozawen_sessions_at = now
        cache = sessions

    for sess in cache:
        if sess.get("payment_status") != "paid":
            continue
        try:
            if _sozawen_derive_key(sess["id"]) == key:
                meta = sess.get("metadata") or {}
                return JSONResponse({
                    "valid": True,
                    "message": "License valid",
                    "email": sess.get("customer_details", {}).get("email") or sess.get("customer_email"),
                    "type": meta.get("type", "self_purchase"),
                    "purchased_at": sess.get("created"),
                })
        except Exception:
            continue

    return JSONResponse({"valid": False, "message": "Key not recognized. If you just purchased, wait 30 seconds and retry."})


# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 9000))
    print(f"Notare Update Server running on http://0.0.0.0:{port}")
    print(f"Admin key: {ADMIN_KEY[:8]}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
