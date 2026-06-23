"""
California earthquake sequence plugin.

Finds the largest-magnitude earthquake near California in the trailing window,
determines the surrounding sequence using a magnitude-dependent search radius
(Wells & Coppersmith 1994), fetches the moment tensor, and plots on a cartopy
terrain basemap with a beachball overlay.

config = {
    "type": "earthquakes",
    "days": 60,
    "ca_buffer_km": 100.0,
    "regional_min_mag": 3.0,
    "local_min_mag": 1.0,
    "buffer_factor": 1.5,
    "min_buffer_km": 20.0,
    "max_buffer_km": 300.0,
    "provider": "shaded-relief"   # or "world-terrain" / "opentopomap"
}
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.figure
import numpy as np
import requests
from PIL import Image

from plugins.base import Plugin

USGS_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
_HEADERS = {"User-Agent": "esp-frame-earthquake-plugin/1.0"}
_EPSG_CA = 3310  # California Albers (meters)

_RENDER_CACHE: dict[str, tuple[float, "Image.Image"]] = {}  # key → (ts, image)
_CACHE_TTL = 900  # 15 minutes


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    id: str
    lon: float
    lat: float
    depth_km: float
    mag: float
    magtype: str
    time: datetime
    place: str
    detail_url: str

    @classmethod
    def from_feature(cls, f: dict) -> "Event":
        p = f["properties"]
        lon, lat, depth = f["geometry"]["coordinates"]
        return cls(
            id=f["id"],
            lon=float(lon),
            lat=float(lat),
            depth_km=float(depth) if depth is not None else float("nan"),
            mag=float(p["mag"]) if p.get("mag") is not None else float("nan"),
            magtype=p.get("magType") or "",
            time=datetime.fromtimestamp(p["time"] / 1000.0, tz=timezone.utc),
            place=p.get("place") or "",
            detail_url=p.get("detail") or "",
        )


# --------------------------------------------------------------------------- #
# USGS queries
# --------------------------------------------------------------------------- #
def _usgs_geojson(**params) -> dict:
    params.setdefault("format", "geojson")
    r = requests.get(USGS_QUERY, params=params, headers=_HEADERS, timeout=60)
    if r.status_code == 400:
        raise RuntimeError(
            "USGS returned 400 (too many events). "
            "Raise min-magnitude threshold and retry.\n" + r.text[:300]
        )
    r.raise_for_status()
    return r.json()


def _query_box(start, end, bbox, min_mag) -> list[Event]:
    data = _usgs_geojson(
        starttime=start.strftime("%Y-%m-%dT%H:%M:%S"),
        endtime=end.strftime("%Y-%m-%dT%H:%M:%S"),
        minlongitude=bbox[0],
        minlatitude=bbox[1],
        maxlongitude=bbox[2],
        maxlatitude=bbox[3],
        minmagnitude=min_mag,
        orderby="magnitude",
    )
    return [Event.from_feature(f) for f in data["features"]]


def _query_circle(start, end, lat, lon, radius_km, min_mag) -> list[Event]:
    data = _usgs_geojson(
        starttime=start.strftime("%Y-%m-%dT%H:%M:%S"),
        endtime=end.strftime("%Y-%m-%dT%H:%M:%S"),
        latitude=lat,
        longitude=lon,
        maxradiuskm=radius_km,
        minmagnitude=min_mag,
        orderby="time-asc",
    )
    return [Event.from_feature(f) for f in data["features"]]


# --------------------------------------------------------------------------- #
# California polygon + buffer
# --------------------------------------------------------------------------- #
def _california_buffer(buffer_km: float):
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform as shp_transform, unary_union
    from shapely.prepared import prep

    to3310 = Transformer.from_crs(4326, _EPSG_CA, always_xy=True)
    to4326 = Transformer.from_crs(_EPSG_CA, 4326, always_xy=True)

    try:
        import cartopy.io.shapereader as shpreader

        shp = shpreader.natural_earth(
            resolution="10m",
            category="cultural",
            name="admin_1_states_provinces_lakes",
        )
        geoms = []
        for rec in shpreader.Reader(shp).records():
            a = rec.attributes
            name = a.get("name") or a.get("name_en") or a.get("gn_name") or ""
            if str(name).lower() == "california":
                geoms.append(rec.geometry)
        if not geoms:
            raise RuntimeError("California not found in Natural Earth states.")
        ca_lonlat = unary_union(geoms)
    except Exception as exc:
        warnings.warn(
            f"Natural Earth CA polygon unavailable ({exc}); falling back to coarse bounding box."
        )
        ca_lonlat = box(-124.55, 32.45, -114.05, 42.05)

    ca_3310 = shp_transform(lambda x, y, z=None: to3310.transform(x, y), ca_lonlat)
    buffered_3310 = ca_3310.buffer(buffer_km * 1000.0)
    buffered_lonlat = shp_transform(
        lambda x, y, z=None: to4326.transform(x, y), buffered_3310
    )
    return buffered_3310, prep(buffered_3310), buffered_lonlat.bounds, to3310


def _filter_inside(events, prepared_3310, to3310) -> list[Event]:
    from shapely.geometry import Point

    out = []
    for e in events:
        x, y = to3310.transform(e.lon, e.lat)
        if prepared_3310.contains(Point(x, y)):
            out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Magnitude-dependent buffer (Wells & Coppersmith 1994)
# --------------------------------------------------------------------------- #
def _magnitude_buffer_km(mag: float, factor: float = 1.5,
                          min_km: float = 20.0, max_km: float = 300.0) -> float:
    rld = 10.0 ** (-2.44 + 0.59 * mag)
    return float(min(max_km, max(min_km, factor * rld)))


# --------------------------------------------------------------------------- #
# Moment tensor
# --------------------------------------------------------------------------- #
def _fetch_focal_mechanism(detail_url: str):
    if not detail_url:
        return None, None, {}
    try:
        detail = requests.get(detail_url, headers=_HEADERS, timeout=60).json()
    except Exception as exc:
        warnings.warn(f"Could not fetch event detail: {exc}")
        return None, None, {}

    products = detail.get("properties", {}).get("products", {})

    def fnum(d, k):
        v = d.get(k)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for mt in products.get("moment-tensor", []):
        p = mt.get("properties", {})
        comps = [fnum(p, f"tensor-{c}") for c in
                 ("mrr", "mtt", "mpp", "mrt", "mrp", "mtp")]
        if all(c is not None for c in comps):
            meta = {
                "source": p.get("beachball-source") or p.get("derived-magnitude-type"),
                "mw": fnum(p, "derived-magnitude"),
                "type": p.get("derived-magnitude-type"),
                "depth_km": fnum(p, "derived-depth"),
            }
            return "mt", comps, meta

    for fm in products.get("focal-mechanism", []):
        p = fm.get("properties", {})
        s = fnum(p, "nodal-plane-1-strike")
        d = fnum(p, "nodal-plane-1-dip")
        r = fnum(p, "nodal-plane-1-rake")
        if None not in (s, d, r):
            return "np", [s, d, r], {"source": p.get("eventsource")}

    return None, None, {}


# --------------------------------------------------------------------------- #
# Terrain tiles
# --------------------------------------------------------------------------- #
_TILE_URLS = {
    "shaded-relief":     "https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
    "world-terrain":     "https://server.arcgisonline.com/ArcGIS/rest/services/World_Terrain_Base/MapServer/tile/{z}/{y}/{x}",
    "esri-topo":         "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    "natgeo":            "https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}",
    "usgs-topo":         "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
    "usgs-imagery-topo": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
    "opentopomap":       "https://tile.opentopomap.org/{z}/{x}/{y}.png",
}


def _terrain_tiles(provider: str):
    import cartopy.io.img_tiles as cimgt
    return cimgt.GoogleTiles(url=_TILE_URLS[provider])


def _tile_zoom(width_deg: float) -> int:
    z = int(round(math.log2(360.0 / max(width_deg, 1e-3)))) + 1
    return max(5, min(12, z))


def _mag_to_size(m):
    return 14.0 * np.power(2.0, np.clip(m, 0.0, 8.0))


# --------------------------------------------------------------------------- #
# Plugin
# --------------------------------------------------------------------------- #
class EarthquakePlugin(Plugin):
    plugin_type = "earthquakes"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.days: int = config.get("days", 60)
        self.ca_buffer_km: float = config.get("ca_buffer_km", 100.0)
        self.regional_min_mag: float = config.get("regional_min_mag", 3.0)
        self.local_min_mag: float = config.get("local_min_mag", 1.0)
        self.buffer_factor: float = config.get("buffer_factor", 1.5)
        self.min_buffer_km: float = config.get("min_buffer_km", 20.0)
        self.max_buffer_km: float = config.get("max_buffer_km", 300.0)
        self.provider: str = config.get("provider", "usgs-topo")

    def render(self) -> Image.Image:
        key = hashlib.md5(
            json.dumps(self.config, sort_keys=True).encode()
        ).hexdigest()
        now = time.time()
        if key in _RENDER_CACHE:
            ts, img = _RENDER_CACHE[key]
            if now - ts < _CACHE_TTL:
                return img
        try:
            mainshock, sequence, radius_km, kind, fm, meta = self._fetch_data()
            result = self._draw(mainshock, sequence, radius_km, kind, fm, meta)
        except Exception as exc:
            return self._error_image(str(exc))
        _RENDER_CACHE[key] = (now, result)
        return result

    def _fetch_data(self):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.days)
        _, prepared, bbox, to3310 = _california_buffer(self.ca_buffer_km)
        candidates = _query_box(start, end, bbox, self.regional_min_mag)
        inside = _filter_inside(candidates, prepared, to3310)
        if not inside:
            raise RuntimeError(
                f"No M≥{self.regional_min_mag} events found inside CA buffer "
                f"in the last {self.days} days."
            )
        mainshock = max(inside, key=lambda e: e.mag)
        radius_km = _magnitude_buffer_km(
            mainshock.mag, self.buffer_factor, self.min_buffer_km, self.max_buffer_km
        )
        sequence = _query_circle(
            start, end, mainshock.lat, mainshock.lon, radius_km, self.local_min_mag
        )
        kind, fm, meta = _fetch_focal_mechanism(mainshock.detail_url)
        return mainshock, sequence, radius_km, kind, fm, meta

    def _draw(self, mainshock: Event, events: list[Event], radius_km: float,
              mech_kind, fm, mech_meta) -> Image.Image:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        import cartopy.geodesic as cgeo
        from matplotlib.colors import Normalize
        from matplotlib.lines import Line2D

        pc = ccrs.PlateCarree()
        merc = ccrs.GOOGLE_MERCATOR

        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * math.cos(math.radians(mainshock.lat)))
        margin = 1.15
        extent = [
            mainshock.lon - dlon * margin, mainshock.lon + dlon * margin,
            mainshock.lat - dlat * margin, mainshock.lat + dlat * margin,
        ]

        fig = matplotlib.figure.Figure(figsize=(12, 16))
        ax = fig.add_subplot(1, 1, 1, projection=merc)
        ax.set_extent(extent, crs=pc)

        try:
            tiles = _terrain_tiles(self.provider)
            ax.add_image(tiles, _tile_zoom(extent[1] - extent[0]),
                         interpolation="bilinear")
        except Exception as exc:
            warnings.warn(f"Terrain tiles unavailable ({exc}); using plain features.")
            ax.add_feature(cfeature.LAND, facecolor="#e8e4dc")
            ax.add_feature(cfeature.OCEAN, facecolor="#cfe2f3")
            ax.add_feature(cfeature.COASTLINE, linewidth=0.6)
            ax.add_feature(cfeature.STATES, linewidth=0.4, edgecolor="0.4")

        # Sort ascending by magnitude so larger events render on top
        events_sorted = sorted(events, key=lambda e: e.mag)
        lons = np.array([e.lon for e in events_sorted])
        lats = np.array([e.lat for e in events_sorted])
        mags = np.array([e.mag for e in events_sorted])

        now = datetime.now(timezone.utc)
        days_ago = np.array(
            [(now - e.time).total_seconds() / 86400.0 for e in events_sorted]
        )
        # Red = recent (today = 0), white = oldest
        norm = Normalize(vmin=0, vmax=max(float(days_ago.max()), 1e-3))
        cmap = "Reds_r"

        sc = ax.scatter(
            lons, lats, transform=pc,
            s=_mag_to_size(mags), c=days_ago, cmap=cmap, norm=norm,
            edgecolor="k", linewidth=0.3, alpha=0.85, zorder=5,
        )
        # cb = fig.colorbar(sc, ax=ax, shrink=0.7, pad=0.02)
        # cb.set_label("Days before today", fontsize=16)
        # cb.ax.tick_params(labelsize=14)

        circle = np.asarray(
            cgeo.Geodesic().circle(mainshock.lon, mainshock.lat,
                                   radius_km * 1000.0, n_samples=181)
        )
        ax.plot(circle[:, 0], circle[:, 1], transform=pc,
                color="0.2", linewidth=1.2, linestyle="--", zorder=4)

        mx, my = merc.transform_point(mainshock.lon, mainshock.lat, pc)
        x0, x1, _, _ = ax.get_extent(crs=merc)
        # Derive beachball diameter from the same _mag_to_size scale as scatter.
        # Scatter s is in points^2; radius in points = sqrt(s/pi).
        # Figure is 12 in wide; axes occupies ~82% → ~708 display points across
        # the data extent (x1-x0 meters), giving meters-per-point conversion.
        r_pts = math.sqrt(float(_mag_to_size(mainshock.mag)) / math.pi)
        bb_width = 2 * r_pts * (x1 - x0) / (0.82 * 12 * 72)
        placed_beachball = False
        if mech_kind in ("mt", "np") and fm is not None:
            try:
                from obspy.imaging.mopad_wrapper import beach

                bball = beach(
                    np.array(fm, dtype=np.float64), xy=(mx, my), width=bb_width,
                    facecolor="firebrick", edgecolor="k", linewidth=0.8,
                    bgcolor="white", zorder=10,
                )
                ax.add_collection(bball)
                placed_beachball = True
            except Exception as exc:
                warnings.warn(f"Beachball render failed ({exc}); using star marker.")
        if not placed_beachball:
            ax.scatter([mainshock.lon], [mainshock.lat], transform=pc,
                       marker="*", s=_mag_to_size(mainshock.mag),
                       facecolor="firebrick", edgecolor="k", linewidth=1.0, zorder=10)

        handles = [
            Line2D([0], [0], marker="o", linestyle="", markerfacecolor="0.6",
                   markeredgecolor="k", markersize=math.sqrt(_mag_to_size(m)),
                   label=f"M {m}")
            for m in (2, 3, 4, 5)
        ]
        handles.append(Line2D([0], [0], color="0.2", linestyle="--",
                               label=f"{radius_km:.0f} km buffer"))
        leg = ax.legend(handles=handles, loc="lower left", title="Magnitude",
                        framealpha=0.9, fontsize=14)
        leg.get_title().set_fontsize(15)

        ax.gridlines(draw_labels=False, linewidth=0.3, color="0.5", alpha=0.5)

        ax.set_title(
            f"Mainshock: {mainshock.mag:.1f} {mainshock.magtype} — {mainshock.place}\n"
            f"{mainshock.time:%Y-%m-%d %H:%M UTC}  |  depth {mainshock.depth_km:.1f} km"
            f"  |  {len(events)} events within {radius_km:.0f} km",
            fontsize=18,
        )

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        return Image.open(buf).convert("RGB")

    def _error_image(self, msg: str) -> Image.Image:
        import textwrap
        from PIL import ImageDraw, ImageFont

        img = Image.new("RGB", (1200, 1600), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48
            )
        except OSError:
            font = ImageFont.load_default()
        draw.text((60, 60), "Earthquake fetch failed:", font=font, fill=(255, 0, 0))
        for i, line in enumerate(textwrap.wrap(msg, 40)):
            draw.text((60, 140 + i * 60), line, font=font, fill=(255, 255, 255))
        return img
