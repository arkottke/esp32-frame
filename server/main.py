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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dither import image_to_epd, epd_to_image
from plugins.base import Plugin

BASE_DIR   = Path(__file__).parent
# CONFIG_PATH lives inside a bind-mounted *directory* (not a single-file mount),
# so the atomic tmp->config rename in _save_config() works. Overridable via env.
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config" / "config.json"))
UPLOADS_DIR = BASE_DIR / "uploads"
TEMPLATES_DIR = BASE_DIR / "templates"

UPLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ESP-Frame Server")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Display timezone for the web UI. `last_seen` is stored in UTC; the frontend
# renders it in this zone (set via the TZ env var, e.g. TZ=America/Los_Angeles).
_TZ_NAME = os.environ.get("TZ", "UTC")
try:
    LOCAL_TZ = ZoneInfo(_TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    LOCAL_TZ = timezone.utc
    _TZ_NAME = "UTC"


def _fmt_local(iso_str: str | None) -> str:
    """Format a stored UTC ISO timestamp as 'YYYY-MM-DD HH:MM' in LOCAL_TZ."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def _pl_label(item: dict) -> str:
    t = item.get("type", "")
    if t == "static":      return item.get("filename") or "(blank white)"
    if t == "weather":     return item.get("location_name") or f"{item.get('lat',0)}, {item.get('lon',0)}"
    if t == "earthquakes": return f"{item.get('days',60)}d / regional M≥{item.get('regional_min_mag',3.0)}"
    return t

templates.env.globals["_pl_label"] = _pl_label
templates.env.globals["_fmt_local"] = _fmt_local
templates.env.globals["_tz_name"] = _TZ_NAME


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
            "soh": None,
        }
    return config["frames"][mac]


# ---------------------------------------------------------------------------
# Image endpoint — called by firmware
# ---------------------------------------------------------------------------

@app.get("/frame/{mac}/image")
async def get_frame_image(mac: str, request: Request) -> Response:
    """
    Return the 960,000-byte EPD binary for the given frame MAC address.
    Auto-registers unknown frames with a blank white image.
    Accepts optional SoH query params: rssi, voltage_mv, wakeup, fw_ver, heap.
    """
    config = _load_config()
    frame = _get_or_create_frame(config, mac)

    # Update last_seen
    frame["last_seen"] = datetime.now(timezone.utc).isoformat()

    # Update state-of-health if any SoH params were sent
    q = request.query_params
    soh_keys = ("rssi", "voltage_mv", "wakeup", "fw_ver", "heap")
    if any(k in q for k in soh_keys):
        def _int(key: str):
            try: return int(q[key])
            except (KeyError, ValueError): return None
        frame["soh"] = {
            "rssi":       _int("rssi"),
            "voltage_mv": _int("voltage_mv"),
            "wakeup":     q.get("wakeup"),
            "fw_ver":     q.get("fw_ver"),
            "heap":       _int("heap"),
        }

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
    return templates.TemplateResponse(request, "index.html", {
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
        allowed = {"static", "playlist", "weather", "weather_ensemble", "earthquakes", "gallery"}
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


@app.delete("/api/frame/{mac}")
async def api_delete_frame(mac: str) -> JSONResponse:
    config = _load_config()
    if mac not in config.get("frames", {}):
        raise HTTPException(status_code=404, detail="Frame not found")
    del config["frames"][mac]
    _save_config(config)
    return JSONResponse({"ok": True})


def _render_frame_image(mac: str) -> "Image.Image":
    """Shared helper: load config, instantiate plugin, render. Raises HTTPException on failure."""
    from PIL import Image, ImageDraw
    config = _load_config()
    frames = config.get("frames", {})
    if mac not in frames:
        raise HTTPException(status_code=404, detail="Frame not found")
    plugin_cfg = frames[mac].get("plugin", {"type": "static", "filename": ""})
    try:
        plugin = Plugin.from_config(plugin_cfg)
        return plugin.render()
    except Exception as exc:
        img = Image.new("RGB", (1200, 1600), (255, 245, 245))
        draw = ImageDraw.Draw(img)
        draw.text((60, 60), f"Render error:\n{exc}", fill=(180, 0, 0))
        return img


@app.get("/api/frame/{mac}/preview.png")
async def api_frame_preview(mac: str) -> StreamingResponse:
    """Render current plugin and return as PNG (no dithering)."""
    import io
    img = _render_frame_image(mac)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/api/frame/{mac}/preview-dithered.png")
async def api_frame_preview_dithered(mac: str) -> StreamingResponse:
    """Render current plugin, dither to EPD palette, decode back to PNG."""
    import io
    img = _render_frame_image(mac)
    epd_bytes = image_to_epd(img)
    dithered = epd_to_image(epd_bytes)
    buf = io.BytesIO()
    dithered.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.delete("/api/upload/{filename}")
async def api_delete_upload(filename: str) -> JSONResponse:
    safe = (UPLOADS_DIR / Path(filename).name).resolve()
    if not str(safe).startswith(str(UPLOADS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not safe.exists():
        raise HTTPException(status_code=404, detail="File not found")
    safe.unlink()
    return JSONResponse({"ok": True})


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
                "type": "gallery",
                "label": "Gallery slideshow",
                "fields": [
                    {"name": "directories", "label": "Directories (one absolute path per line)", "type": "textarea"},
                    {"name": "zoom",        "label": "Zoom mode", "type": "select",
                     "options": ["fill", "fit", "stretch", "center"]},
                ],
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
                "type": "weather_ensemble",
                "label": "Ensemble weather forecast",
                "fields": [
                    {"name": "lat",             "label": "Latitude",                                                          "type": "number"},
                    {"name": "lon",             "label": "Longitude",                                                         "type": "number"},
                    {"name": "location_name",   "label": "Location name",                                                     "type": "text"},
                    {"name": "days",            "label": "Days (1-10)",                                                       "type": "number"},
                    {"name": "temperature_unit","label": "Temperature unit (fahrenheit / celsius)",                           "type": "text"},
                    {"name": "model",           "label": "Model (ecmwf_ifs025 / gfs_seamless / icon_seamless)",               "type": "text"},
                    {"name": "yellow_variable", "label": "Yellow panel (wind_gusts_10m / cloud_cover / surface_pressure)",    "type": "text"},
                ],
            },
            {
                "type": "earthquakes",
                "label": "CA earthquake sequence",
                "fields": [
                    {"name": "days",             "label": "Trailing window (days)",       "type": "number"},
                    {"name": "regional_min_mag", "label": "Regional min magnitude",       "type": "number"},
                    {"name": "local_min_mag",    "label": "Sequence min magnitude",       "type": "number"},
                    {"name": "provider",         "label": "Basemap (usgs-topo / usgs-imagery-topo / natgeo / esri-topo / opentopomap / shaded-relief / world-terrain)", "type": "text"},
                ],
            },
        ]
    })


_DIR_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


@app.get("/api/directory/files")
async def api_directory_files(path: str) -> JSONResponse:
    """List image files in an arbitrary directory, sorted by mtime."""
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    files = []
    for f in p.iterdir():
        if f.is_file() and f.suffix.lower() in _DIR_IMAGE_EXTS:
            files.append({"name": f.name, "path": str(f), "mtime": int(f.stat().st_mtime)})
    files.sort(key=lambda x: x["mtime"])
    return JSONResponse({"files": files})


@app.get("/api/directory/image")
async def api_directory_image(path: str, size: int = 150) -> StreamingResponse:
    """Serve a square JPEG thumbnail for an image file."""
    import io
    from PIL import Image, ImageOps
    size = max(50, min(size, 800))
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in _DIR_IMAGE_EXTS:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        img = Image.open(p).convert("RGB")
        thumb = ImageOps.fit(img, (size, size), Image.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load image: {exc}")
