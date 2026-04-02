"""Auto-update plugin — pulls patches from the remote update server on startup.

This plugin runs ONCE at load time (not per-request). It checks the remote
server, downloads any available update zip, and extracts patchable files
over the local install.

Patchable paths:
  - _internal/plugins/   (plugin code)
  - _internal/static/    (frontend HTML/CSS/JS)
  - static/              (frontend mirror)
  - templates/           (Jinja templates)
  - app.py               (main application)
  - _internal/app.py     (frozen application copy)
"""
import json
import logging
import sys
import os
from pathlib import Path

logger = logging.getLogger("notare")

_app = sys.modules.get("app")
if not _app:
    raise RuntimeError("app module not loaded")

BASE_DIR = _app.BASE_DIR
SETTINGS = _app.SETTINGS
UPDATE_SERVER = "https://notare-updates.onrender.com"

# Marker file to track that this updated plugin has run at least once
_MIGRATION_MARKER = BASE_DIR / "_internal" / ".ota_v2"


def _run_update(force=False):
    local_version = SETTINGS.get("version", "0.0.0")

    import urllib.request as _ur

    # Check for update
    check_version = "0.0.0" if force else local_version
    check_url = f"{UPDATE_SERVER}/api/update?current={check_version}"
    with _ur.urlopen(check_url, timeout=10) as resp:
        data = json.loads(resp.read())

    if not data.get("update_available"):
        logger.info("OTA: up to date (v%s)", local_version)
        return

    server_version = data.get("version", "0.0.0")
    download_url = data.get("url", "")
    if download_url.startswith("/"):
        download_url = UPDATE_SERVER + download_url
    elif not download_url.startswith("http"):
        download_url = f"{UPDATE_SERVER}/api/update/download/{server_version}"

    logger.info("OTA: update available v%s -> v%s, downloading...", local_version, server_version)

    # Download
    import tempfile as _tf
    tmp = Path(_tf.mktemp(suffix=".zip", prefix="notare_update_"))
    _ur.urlretrieve(download_url, str(tmp))
    logger.info("OTA: downloaded %s (%d bytes)", tmp.name, tmp.stat().st_size)

    # Extract safe files only
    import zipfile as _zf
    _safe_prefixes = ("_internal/plugins/", "_internal/static/", "static/", "templates/")
    _safe_files = ("app.py", "_internal/app.py")
    extracted = 0
    skipped = []
    with _zf.ZipFile(str(tmp), "r") as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in member:
                continue
            allowed = any(member.startswith(p) for p in _safe_prefixes) or member in _safe_files
            if allowed:
                target = BASE_DIR / member
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                extracted += 1
            else:
                skipped.append(member)

    logger.info("OTA: extracted %d files from v%s", extracted, server_version)
    if skipped:
        logger.info("OTA: skipped %d files not in safe list: %s", len(skipped), skipped)

    # Update version
    SETTINGS["version"] = server_version
    SETTINGS["last_update"] = __import__("datetime").datetime.now().isoformat()
    SETTINGS["updated_silently"] = True
    _app._save_settings()

    try:
        tmp.unlink()
    except Exception:
        pass

    logger.info("OTA: updated to v%s successfully", server_version)


# Run on plugin load — errors are non-fatal
try:
    # Migration: if this is the first time the v2 plugin runs, force a full
    # re-download to pick up app.py (which the old plugin couldn't extract).
    if not _MIGRATION_MARKER.exists():
        logger.info("OTA: first run of updated plugin — forcing full re-download")
        _run_update(force=True)
        try:
            _MIGRATION_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _MIGRATION_MARKER.write_text("v2")
        except Exception:
            pass
    else:
        _run_update()
except Exception as e:
    logger.debug("OTA: update check failed (non-critical): %s", e)

print("Plugin loaded: auto_update.py — OTA auto-update active")
