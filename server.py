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

    return JSONResponse({"update_available": False, "version": server_version})


@app.get("/api/update/download/{version}")
async def download_update(version: str):
    """Serve the update zip for a specific version."""
    pkg = UPDATES_DIR / f"notare-{version}.zip"
    if pkg.exists():
        return FileResponse(str(pkg), filename=f"notare-{version}.zip",
                            media_type="application/zip")
    return JSONResponse({"error": "Package not found"}, status_code=404)


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
