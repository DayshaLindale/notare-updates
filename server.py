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

ADMIN_KEY = os.environ.get("ADMIN_KEY", "notare-update-admin-2026")

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
    """POST-based license validation — used by notare.py remote fallback."""
    try:
        data = await request.json()
        key = data.get("key", "")
    except Exception:
        return JSONResponse({"valid": False, "reason": "bad_request"})
    if not key:
        return JSONResponse({"valid": False, "reason": "no_key"})

    admin_file = BASE_DIR / "admin_data.json"
    if not admin_file.exists():
        return JSONResponse({"valid": False, "reason": "no_database"})

    try:
        admin_data = json.loads(admin_file.read_text(encoding="utf-8"))
        for lic in admin_data.get("licenses", []):
            if lic.get("key", "").upper() == key.upper() and lic.get("status") == "active":
                exp = lic.get("expires", "")
                if exp and exp != "":
                    from datetime import datetime as _dt
                    if exp < _dt.now().strftime("%Y-%m-%d"):
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


@app.get("/api/download-installer")
async def download_installer():
    """Redirect to the installer download. User sees notarelegal.com URL, gets the file from GitHub."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        "https://github.com/DayshaLindale/notare-updates/releases/download/v1.0.0/NotareSetup_v1.0.0.exe",
        # v1.0.0: Disk-loaded app.py for reliable OTA, preview pane, profile merge, all fixes
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
# Local development
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 9000))
    print(f"Notare Update Server running on http://0.0.0.0:{port}")
    print(f"Admin key: {ADMIN_KEY[:8]}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
