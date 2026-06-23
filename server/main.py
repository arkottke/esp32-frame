"""
ESP-Frame server.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dither import image_to_epd
from plugins.base import Plugin

BASE_DIR   = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
UPLOADS_DIR = BASE_DIR / "uploads"
TEMPLATES_DIR = BASE_DIR / "templates"

UPLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ESP-Frame Server")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"frames": {}}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save_config(config: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(config, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)
    tmp.replace(CONFIG_PATH)


def _get_or_create_frame(config: dict, mac: str) -> dict[str, Any]:
    """Return the frame entry, auto-creating it on first contact."""
    if mac not in config["frames"]:
        config["frames"][mac] = {
            "name": mac,
            "plugin": {"type": "static", "filename": ""},
            "poll_seconds": 600,
            "last_seen": None,
        }
    return config["frames"][mac]


# ---------------------------------------------------------------------------
# Image endpoint — called by firmware
# ---------------------------------------------------------------------------

@app.get("/frame/{mac}/image")
async def get_frame_image(mac: str) -> Response:
    """
    Return the 960,000-byte EPD binary for the given frame MAC address.
    Auto-registers unknown frames with a blank white image.
    """
    config = _load_config()
    frame = _get_or_create_frame(config, mac)

    # Update last_seen
    frame["last_seen"] = datetime.now(timezone.utc).isoformat()
    _save_config(config)

    plugin_config = frame.get("plugin", {"type": "static", "filename": ""})
    plugin = Plugin.from_config(plugin_config)

    try:
        pil_img = plugin.render()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Plugin render failed: {exc}")

    # If plugin is a playlist, persist the advanced index back to disk
    if plugin_config.get("type") == "playlist":
        _save_config(config)

    epd_bytes = image_to_epd(pil_img)
    return Response(content=epd_bytes, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    config = _load_config()
    frames = list(config.get("frames", {}).items())
    uploads = sorted(f.name for f in UPLOADS_DIR.iterdir() if f.is_file() and not f.name.startswith("."))
    return templates.TemplateResponse("index.html", {
        "request": request,
        "frames": frames,
        "uploads": uploads,
    })


# ---------------------------------------------------------------------------
# REST API for the web UI
# ---------------------------------------------------------------------------

@app.get("/api/frames")
async def api_list_frames() -> JSONResponse:
    config = _load_config()
    return JSONResponse(config.get("frames", {}))


@app.post("/api/frame/{mac}")
async def api_update_frame(mac: str, request: Request) -> JSONResponse:
    """Update frame configuration (name, plugin, poll_seconds)."""
    body = await request.json()
    config = _load_config()
    frame = _get_or_create_frame(config, mac)

    if "name" in body:
        frame["name"] = str(body["name"])[:64]
    if "plugin" in body:
        # Validate plugin type exists
        plugin_type = body["plugin"].get("type", "static")
        allowed = {"static", "playlist", "weather", "earthquakes"}
        if plugin_type not in allowed:
            raise HTTPException(status_code=400, detail=f"Unknown plugin type: {plugin_type!r}")
        frame["plugin"] = body["plugin"]
    if "poll_seconds" in body:
        try:
            frame["poll_seconds"] = max(60, int(body["poll_seconds"]))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="poll_seconds must be an integer")

    _save_config(config)
    return JSONResponse(frame)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> JSONResponse:
    """Upload an image to the uploads/ directory."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    safe_name = Path(file.filename or "upload").name
    dest = UPLOADS_DIR / safe_name
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return JSONResponse({"filename": safe_name})


@app.get("/api/plugins")
async def api_list_plugins() -> JSONResponse:
    return JSONResponse({
        "plugins": [
            {
                "type": "static",
                "label": "Static image",
                "fields": [{"name": "filename", "label": "Image file", "type": "select_upload"}],
            },
            {
                "type": "playlist",
                "label": "Playlist",
                "fields": [{"name": "items", "label": "Items (JSON array of plugin configs)", "type": "textarea"}],
            },
            {
                "type": "weather",
                "label": "Weather forecast",
                "fields": [
                    {"name": "lat",           "label": "Latitude",      "type": "number"},
                    {"name": "lon",           "label": "Longitude",     "type": "number"},
                    {"name": "location_name", "label": "Location name", "type": "text"},
                    {"name": "days",          "label": "Days (1-7)",    "type": "number"},
                ],
            },
            {
                "type": "earthquakes",
                "label": "Earthquake map",
                "fields": [
                    {"name": "days",          "label": "Period (1, 7, or 30)", "type": "number"},
                    {"name": "min_magnitude", "label": "Min magnitude",        "type": "number"},
                ],
            },
        ]
    })
