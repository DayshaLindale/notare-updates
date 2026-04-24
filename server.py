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
from datetime import datetime, timedelta
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

            # Return entitlements so the Notare app can show/hide workspaces
            # and proof profiles based on what the customer actually bought.
            # Entitlements default to an empty shape if not set on the license
            # (old licenses from before per-workspace entitlements were added).
            entitlements = lic.get("entitlements") or {}
            return JSONResponse({
                "valid": True,
                "tier": lic.get("tier", "solo"),
                "expires": lic.get("expires", ""),
                "customer": lic.get("customer_name", ""),
                "bound": bool(lic.get("machine_id")),
                "entitlements": {
                    "workspaces":     entitlements.get("workspaces", []),
                    "proof_profiles": entitlements.get("proof_profiles", []),
                },
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
# Admin CRUD endpoints — called by /static/admin.html via authFetch.
# Auth: for now we reuse the ADMIN_KEY bearer check. Long-term this should
# be per-admin sessions from an admin-login endpoint.
# ---------------------------------------------------------------------------

def _load_admin_data():
    admin_file = BASE_DIR / "admin_data.json"
    if not admin_file.exists():
        return {"admins": [], "licenses": []}
    try:
        return json.loads(admin_file.read_text(encoding="utf-8"))
    except Exception:
        return {"admins": [], "licenses": []}


def _save_admin_data(data):
    admin_file = BASE_DIR / "admin_data.json"
    admin_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _generate_license_key():
    """Generate a NOTARE-XXXXXXXX-XXXXXXXX-XXXXXXXX key. Uniqueness is
    enforced by the admin_data.json uniqueness check in generate_license."""
    import secrets
    def chunk():
        return ''.join(secrets.choice("0123456789ABCDEF") for _ in range(8))
    return f"NOTARE-{chunk()}-{chunk()}-{chunk()}"


@app.post("/api/admin/license/generate")
async def admin_license_generate(request: Request):
    """Generate a new license key with workspace + profile entitlements.
    Called by the admin panel's New License modal."""
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_name = (data.get("customer_name") or "").strip()
    email         = (data.get("email") or "").strip()
    org           = (data.get("org") or "").strip()
    tier          = (data.get("tier") or "proofing_5").strip()
    duration_days = int(data.get("duration_days") or 365)
    entitlements  = data.get("entitlements") or {}
    # Shape the entitlements object defensively — only two keys are used.
    entitlements = {
        "workspaces":     [str(x) for x in (entitlements.get("workspaces") or [])],
        "proof_profiles": [str(x) for x in (entitlements.get("proof_profiles") or [])],
    }

    if not customer_name:
        return JSONResponse({"error": "customer_name is required"}, status_code=400)

    admin_data = _load_admin_data()
    existing_keys = {l.get("key", "").upper() for l in admin_data.get("licenses", [])}

    # Generate a unique key
    for _ in range(50):
        candidate = _generate_license_key()
        if candidate.upper() not in existing_keys:
            break
    else:
        return JSONResponse({"error": "Could not generate a unique key"}, status_code=500)

    # Compute expires
    if duration_days and duration_days > 0:
        expires_dt = datetime.now() + timedelta(days=duration_days)
        expires_str = expires_dt.strftime("%Y-%m-%d")
    else:
        expires_str = ""  # unlimited

    new_lic = {
        "key":           candidate,
        "customer_name": customer_name,
        "email":         email,
        "org":           org,
        "tier":          tier,
        "status":        "active",
        "created":       datetime.now().strftime("%Y-%m-%d"),
        "expires":       expires_str,
        "entitlements":  entitlements,
    }
    admin_data.setdefault("licenses", []).append(new_lic)
    _save_admin_data(admin_data)

    return JSONResponse({
        "ok": True,
        "key": candidate,
        "customer_name": customer_name,
        "tier": tier,
        "expires": expires_str,
        "entitlements": entitlements,
    })


@app.get("/api/admin/licenses")
async def admin_list_licenses(request: Request):
    """List every license (for the admin panel table)."""
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    data = _load_admin_data()
    return JSONResponse({"licenses": data.get("licenses", [])})


@app.post("/api/admin/license/{key}/deactivate")
async def admin_license_deactivate(key: str, request: Request):
    """Flip a license's status to 'inactive' (and release machine binding)."""
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    admin_data = _load_admin_data()
    for lic in admin_data.get("licenses", []):
        if lic.get("key", "").upper() == key.upper():
            lic["status"] = "inactive"
            lic.pop("machine_id", None)
            _save_admin_data(admin_data)
            return JSONResponse({"ok": True, "key": key})
    return JSONResponse({"error": "Key not found"}, status_code=404)


@app.post("/api/admin/license/{key}/activate")
async def admin_license_activate(key: str, request: Request):
    """Flip a license's status back to 'active'."""
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    admin_data = _load_admin_data()
    for lic in admin_data.get("licenses", []):
        if lic.get("key", "").upper() == key.upper():
            lic["status"] = "active"
            _save_admin_data(admin_data)
            return JSONResponse({"ok": True, "key": key})
    return JSONResponse({"error": "Key not found"}, status_code=404)


@app.post("/api/admin/license/{key}/update-entitlements")
async def admin_license_update_entitlements(key: str, request: Request):
    """Change the entitlements on an existing license (upgrade/downgrade)."""
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not _check_admin_key(auth):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    new_ent = {
        "workspaces":     [str(x) for x in (data.get("workspaces") or [])],
        "proof_profiles": [str(x) for x in (data.get("proof_profiles") or [])],
    }
    admin_data = _load_admin_data()
    for lic in admin_data.get("licenses", []):
        if lic.get("key", "").upper() == key.upper():
            lic["entitlements"] = new_ent
            if "tier" in data:
                lic["tier"] = data["tier"]
            _save_admin_data(admin_data)
            return JSONResponse({"ok": True, "key": key, "entitlements": new_ent})
    return JSONResponse({"error": "Key not found"}, status_code=404)


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
async def sozawen_key_for_purchase(session_id: str = "", payment_intent: str = ""):
    """Return the license key for a successful Stripe payment.

    Accepts either a checkout session_id (legacy Payment Links flow) or a
    payment_intent id (embedded Payment Element flow). Verifies the payment is
    real and succeeded with Stripe before computing the key, so the endpoint
    can't be used to fish keys for unpaid or fabricated IDs.
    """
    if payment_intent:
        if not payment_intent.startswith(("pi_live_", "pi_test_")):
            return JSONResponse({"error": "invalid payment_intent"}, status_code=400)
        try:
            pi = _stripe_get(f"/v1/payment_intents/{payment_intent}")
        except _uerr.HTTPError as e:
            return JSONResponse({"error": f"stripe: {e.code}"}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        if pi.get("status") != "succeeded":
            return JSONResponse({"error": "payment not succeeded", "status": pi.get("status")}, status_code=402)
        try:
            key = _sozawen_derive_key(payment_intent)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        meta = pi.get("metadata") or {}
        email = pi.get("receipt_email")
        if not email:
            for ch in (pi.get("charges", {}).get("data") or []):
                email = (ch.get("billing_details") or {}).get("email") or ch.get("receipt_email") or ""
                if email:
                    break
        return JSONResponse({
            "key": key, "email": email,
            "type": meta.get("type", "self_purchase"),
            "pool": meta.get("pool", "revenue"),
            "amount_total": pi.get("amount"),
            "currency": pi.get("currency"),
        })

    if not session_id or not session_id.startswith(("cs_live_", "cs_test_")):
        return JSONResponse({"error": "invalid session_id or payment_intent"}, status_code=400)
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


# ---------------------------------------------------------------------------
# Sozawen — webhook, email delivery, pool ledger
# ---------------------------------------------------------------------------

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")


def _verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verify Stripe webhook signature manually (no stripe SDK dependency).

    Format: t=TIMESTAMP,v1=SIGNATURE[,v0=LEGACY]
    Signed payload is: f"{t}.{raw_body}", HMAC-SHA256 hex, constant-time compare.
    """
    if not secret or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts = parts.get("t", "")
    v1 = parts.get("v1", "")
    if not ts or not v1:
        return False
    try:
        if abs(int(time.time()) - int(ts)) > tolerance:
            return False
    except ValueError:
        return False
    signed = f"{ts}.".encode() + payload
    expected = _hmac.new(secret.encode(), signed, _hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, v1)


def _send_welcome_email(to_email: str, key: str, purchase_type: str = "self_purchase",
                        amount_cents: int | None = None) -> tuple[bool, str]:
    """Send the 'welcome to Sozawen' email with the license key + download link.

    Uses Gmail SMTP over SSL with an app password. Fails silently-but-logged
    so a broken email provider never blocks a webhook response (Stripe would
    retry the whole webhook on 5xx, which isn't what we want).
    """
    if not EMAIL_USER or not EMAIL_APP_PASSWORD:
        return False, "email not configured"

    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if purchase_type == "gift":
        subject = "Thank you for gifting a Sozawen seat"
        body_html = f"""
<html><body style="font-family: -apple-system, Segoe UI, sans-serif; color: #d8d8e8; background: #08080f; padding: 32px; line-height: 1.7;">
  <div style="max-width: 560px; margin: 0 auto;">
    <div style="font-size: 32px; letter-spacing: 10px; font-weight: 200; background: linear-gradient(135deg, #9b59b6, #2dd4a8); -webkit-background-clip: text; color: transparent; margin-bottom: 8px;">SOZAWEN</div>
    <div style="font-style: italic; color: #2dd4a8; margin-bottom: 32px;">Born from the burn. Built by feeling.</div>
    <p>Thank you. Because of you, a musician who couldn't afford Sozawen is going to get it free.</p>
    <p>Your gift reference: <code style="background: rgba(45,212,168,.08); padding: 3px 8px; border-radius: 4px; color: #2dd4a8;">{key[-8:]}</code></p>
    <p>When a seat is claimed from your gift, we'll email you to let you know.</p>
    <p style="color: #7878a0; font-size: 13px; margin-top: 48px;">— Daysha Lindale</p>
  </div>
</body></html>""".strip()
    else:
        subject = "Your Sozawen license key"
        download_url = "https://github.com/DayshaLindale/sozawen/releases/download/v1.0.0/SozawenSetup_v1.0.0.exe"
        body_html = f"""
<html><body style="font-family: -apple-system, Segoe UI, sans-serif; color: #d8d8e8; background: #08080f; padding: 32px; line-height: 1.7;">
  <div style="max-width: 560px; margin: 0 auto;">
    <div style="font-size: 32px; letter-spacing: 10px; font-weight: 200; background: linear-gradient(135deg, #9b59b6, #2dd4a8); -webkit-background-clip: text; color: transparent; margin-bottom: 8px;">SOZAWEN</div>
    <div style="font-style: italic; color: #2dd4a8; margin-bottom: 32px;">Born from the burn. Built by feeling.</div>
    <p>You're in. Thank you for trusting me with your music.</p>
    <div style="background: rgba(45,212,168,.06); border: 1px solid rgba(45,212,168,.2); border-radius: 8px; padding: 18px; margin: 24px 0;">
      <div style="font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: #7878a0; margin-bottom: 8px;">Your license key</div>
      <div style="font-family: 'Consolas', monospace; font-size: 22px; letter-spacing: 3px; color: #2dd4a8;">{key}</div>
    </div>
    <p>
      <a href="{download_url}" style="display: inline-block; padding: 14px 32px; background: linear-gradient(135deg, #9b59b6, #2dd4a8); color: #fff; text-decoration: none; border-radius: 30px; font-weight: 500; letter-spacing: 2px;">Download Sozawen</a>
    </p>
    <p style="color: #7878a0; font-size: 14px;">Windows 10/11, ~1.2 GB. Install, launch, open Preferences, paste your key to activate. The app works without activation too — keys just mark you as a supporter and unlock any future paid-only features.</p>
    <p style="color: #7878a0; font-size: 13px; margin-top: 48px;">Go make the thing.<br>— Daysha Lindale</p>
  </div>
</body></html>""".strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Sozawen <{EMAIL_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(EMAIL_USER, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, str(e)


@app.post("/api/sozawen/stripe-webhook")
async def sozawen_stripe_webhook(request: Request):
    """Receive Stripe events. On checkout.session.completed, derive the key
    and email it to the customer.

    Stripe retries on 5xx responses, so we always return 200 for valid events
    (email failures are logged but not propagated to avoid duplicate sends on
    retry — the thank-you page is the primary delivery path, this email is
    belt-and-suspenders).
    """
    body = await request.body()
    sig = request.headers.get("Stripe-Signature", "")

    if not _verify_stripe_signature(body, sig, STRIPE_WEBHOOK_SECRET):
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    try:
        event = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        # Legacy path — Payment Links flow (Stripe-hosted checkout)
        sid = obj.get("id", "")
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or ""
        meta = obj.get("metadata") or {}
        amount = obj.get("amount_total")
    elif etype == "payment_intent.succeeded":
        # Embedded Payment Element flow (sozawen.com/checkout.html)
        sid = obj.get("id", "")
        # Email comes from either the charge's receipt_email, the PI's receipt_email,
        # or the billing_details on the successful charge.
        email = obj.get("receipt_email") or ""
        if not email:
            charges = (obj.get("charges") or {}).get("data") or []
            if not charges:
                # PaymentIntent may reference latest_charge instead (newer API)
                pass
            for ch in charges:
                email = (ch.get("billing_details") or {}).get("email") or ch.get("receipt_email") or ""
                if email:
                    break
        meta = obj.get("metadata") or {}
        amount = obj.get("amount")
    else:
        return JSONResponse({"ok": True, "ignored": etype})

    ptype = meta.get("type", "self_purchase")

    if not sid or not email:
        return JSONResponse({"ok": True, "skipped": "missing session_id or email"})

    try:
        key = _sozawen_derive_key(sid)
    except Exception as e:
        return JSONResponse({"ok": True, "skipped": f"key error: {e}"})

    # Record in ledger (best-effort; ledger is ephemeral on Render free tier
    # but useful for the pool-balance endpoint between restarts).
    _ledger_append({
        "session_id": sid,
        "key": key,
        "email": email,
        "type": ptype,
        "pool": meta.get("pool", "revenue"),
        "amount_cents": amount,
        "at": int(time.time()),
    })

    ok, msg = _send_welcome_email(email, key, ptype, amount)
    return JSONResponse({"ok": True, "email_sent": ok, "email_msg": msg, "key_tail": key[-8:]})


LEDGER_PATH = Path(os.environ.get("SOZAWEN_LEDGER", "sozawen_ledger.json"))


def _ledger_load() -> list[dict]:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _ledger_append(entry: dict) -> None:
    entries = _ledger_load()
    # Dedupe by session_id so webhook retries don't double-count
    if any(e.get("session_id") == entry.get("session_id") for e in entries):
        return
    entries.append(entry)
    try:
        LEDGER_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/api/sozawen/pool-balance")
async def sozawen_pool_balance(authorization: str = Header(None)):
    """Return the scholarship pool balance — gifts received, seats granted,
    and remaining capacity. Admin-only.
    """
    if not _check_admin_key(authorization):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    entries = _ledger_load()
    gifts = [e for e in entries if e.get("pool") == "scholarship"]
    granted = [e for e in entries if e.get("pool") == "granted"]
    return JSONResponse({
        "gifts_received": len(gifts),
        "gifts_total_cents": sum(int(e.get("amount_cents") or 0) for e in gifts),
        "seats_granted": len(granted),
        "seats_available": len(gifts) - len(granted),
        "entries": entries[-50:],
    })


@app.post("/api/sozawen/create-payment-intent")
async def sozawen_create_payment_intent(request: Request):
    """Create a Stripe PaymentIntent for embedded checkout (Payment Elements).

    Called by /checkout.html on sozawen.com when the page loads. The returned
    client_secret is never a long-lived credential — it only authorizes ONE
    payment for ONE specific intent, scoped to the browser session that
    requested it. Safe to return to the frontend.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    ptype = body.get("type", "self_purchase")
    if ptype not in ("self_purchase", "gift"):
        return JSONResponse({"error": "invalid type"}, status_code=400)
    pool = "scholarship" if ptype == "gift" else "revenue"
    description = (
        "Sozawen — pay-it-forward (funds a free license for a musician in need)"
        if ptype == "gift" else "Sozawen — lifetime license, one-time purchase"
    )

    params = [
        ("amount", "7900"),
        ("currency", "usd"),
        ("automatic_payment_methods[enabled]", "true"),
        ("metadata[type]", ptype),
        ("metadata[pool]", pool),
        ("metadata[product]", "sozawen"),
        ("description", description),
        ("statement_descriptor_suffix", "SOZAWEN"),
    ]
    if not STRIPE_READ_KEY:
        return JSONResponse({"error": "STRIPE_READ_KEY not configured"}, status_code=500)
    auth = _base64.b64encode(f"{STRIPE_READ_KEY}:".encode()).decode()
    req = _ureq.Request(
        "https://api.stripe.com/v1/payment_intents",
        data=_uparse.urlencode(params).encode(),
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with _ureq.urlopen(req, timeout=15) as r:
            pi = json.loads(r.read())
    except _uerr.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err_msg = json.loads(err_body).get("error", {}).get("message", err_body)
        except Exception:
            err_msg = err_body
        return JSONResponse({"error": f"stripe: {err_msg}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({
        "client_secret": pi.get("client_secret"),
        "payment_intent_id": pi.get("id"),
        "amount": pi.get("amount"),
        "currency": pi.get("currency"),
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

    # Cached listings for both sessions (Payment Links flow) and payment intents
    # (embedded Payment Element flow). 10-minute TTL.
    now = time.time()

    sessions = getattr(app.state, "sozawen_sessions", None)
    pis = getattr(app.state, "sozawen_payment_intents", None)
    cache_time = getattr(app.state, "sozawen_sessions_at", 0)

    if sessions is None or pis is None or now - cache_time > 600:
        # Sessions
        sessions = []
        starting_after = None
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

        # Payment Intents
        pis = []
        starting_after = None
        for _ in range(10):
            params = {"limit": "100"}
            if starting_after:
                params["starting_after"] = starting_after
            try:
                page = _stripe_get("/v1/payment_intents", params)
            except Exception:
                break
            page_data = page.get("data", [])
            pis.extend(page_data)
            if not page.get("has_more") or not page_data:
                break
            starting_after = page_data[-1]["id"]

        app.state.sozawen_sessions = sessions
        app.state.sozawen_payment_intents = pis
        app.state.sozawen_sessions_at = now

    # Check checkout sessions
    for sess in sessions:
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

    # Check payment intents
    for pi in pis:
        if pi.get("status") != "succeeded":
            continue
        try:
            if _sozawen_derive_key(pi["id"]) == key:
                meta = pi.get("metadata") or {}
                email = pi.get("receipt_email")
                if not email:
                    for ch in (pi.get("charges", {}).get("data") or []):
                        email = (ch.get("billing_details") or {}).get("email") or ch.get("receipt_email") or ""
                        if email:
                            break
                return JSONResponse({
                    "valid": True,
                    "message": "License valid",
                    "email": email,
                    "type": meta.get("type", "self_purchase"),
                    "purchased_at": pi.get("created"),
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
