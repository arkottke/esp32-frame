#!/usr/bin/env python3
"""
largest_ca_earthquake.py
========================

Find the largest-magnitude earthquake in the last ~2 months within 100 km of
California, pull the surrounding sequence using a *magnitude-dependent* search
buffer, and plot the events on a terrain basemap with the mainshock moment
tensor (beachball) overlaid.

Workflow
--------
1.  Buffer the California state polygon by 100 km (in EPSG:3310, CA Albers, so
    the buffer is in real distance rather than degrees).
2.  Query USGS ComCat over the buffered bounding box for the trailing window,
    then keep only epicenters that actually fall inside the buffered polygon.
3.  Pick the maximum-magnitude event -> "mainshock".
4.  Compute a magnitude-dependent radius from Wells & Coppersmith (1994)
    subsurface rupture length, and re-query ComCat for *all* events within that
    radius of the mainshock (the fore/aftershock sequence).
5.  Fetch the mainshock moment tensor (RTP components, else nodal planes).
6.  Plot on a cartopy terrain basemap: events sized by magnitude, colored by
    time relative to the mainshock, with the beachball and buffer circle.

Data:  USGS ComCat (fdsnws/event/1 + GeoJSON detail feed).
Deps:  requests numpy matplotlib cartopy shapely pyproj obspy
       pip install requests numpy matplotlib cartopy shapely pyproj obspy
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

USGS_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
HEADERS = {"User-Agent": "largest-ca-earthquake/1.0 (personal research script)"}
EPSG_CA = 3310  # California Albers (meters) -- accurate for in-state buffering


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
def usgs_geojson(**params) -> dict:
    params.setdefault("format", "geojson")
    r = requests.get(USGS_QUERY, params=params, headers=HEADERS, timeout=60)
    if r.status_code == 400:
        raise RuntimeError(
            "USGS returned 400 (often 'too many events'). "
            "Raise a min-magnitude threshold and retry.\n" + r.text[:300]
        )
    r.raise_for_status()
    return r.json()


def query_box(start, end, bbox, min_mag) -> list[Event]:
    """bbox = (minlon, minlat, maxlon, maxlat)."""
    data = usgs_geojson(
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


def query_circle(start, end, lat, lon, radius_km, min_mag) -> list[Event]:
    data = usgs_geojson(
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
# California polygon + 100 km buffer
# --------------------------------------------------------------------------- #
def california_buffer(buffer_km: float):
    """
    Return (buffered_polygon_3310, prepared_3310, bbox_lonlat, to3310_transform).

    The polygon is buffered in EPSG:3310 so the buffer distance is true ground
    distance. Falls back to a coarse CA bounding box if Natural Earth is
    unavailable.
    """
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform as shp_transform, unary_union
    from shapely.prepared import prep

    to3310 = Transformer.from_crs(4326, EPSG_CA, always_xy=True)
    to4326 = Transformer.from_crs(EPSG_CA, 4326, always_xy=True)

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
    except Exception as exc:  # pragma: no cover - network/data dependent
        print(f"[warn] Natural Earth CA polygon unavailable ({exc}); "
              "falling back to a coarse CA bounding box.", file=sys.stderr)
        ca_lonlat = box(-124.55, 32.45, -114.05, 42.05)

    ca_3310 = shp_transform(lambda x, y, z=None: to3310.transform(x, y), ca_lonlat)
    buffered_3310 = ca_3310.buffer(buffer_km * 1000.0)
    buffered_lonlat = shp_transform(
        lambda x, y, z=None: to4326.transform(x, y), buffered_3310
    )
    return buffered_3310, prep(buffered_3310), buffered_lonlat.bounds, to3310


def filter_inside(events, prepared_3310, to3310):
    from shapely.geometry import Point

    out = []
    for e in events:
        x, y = to3310.transform(e.lon, e.lat)
        if prepared_3310.contains(Point(x, y)):
            out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Magnitude-dependent buffer
# --------------------------------------------------------------------------- #
def magnitude_buffer_km(mag: float, factor: float = 1.5, min_km: float = 20.0,
                        max_km: float = 300.0) -> float:
    """
    Search radius for the surrounding sequence, scaled with rupture dimension.

    Uses Wells & Coppersmith (1994) subsurface rupture length (all slip types):
        log10(RLD[km]) = -2.44 + 0.59 * M
    Aftershock zones extend roughly a rupture length beyond the rupture, so we
    take radius = factor * RLD, clamped to [min_km, max_km].
    """
    rld = 10.0 ** (-2.44 + 0.59 * mag)
    return float(min(max_km, max(min_km, factor * rld)))


# --------------------------------------------------------------------------- #
# Moment tensor
# --------------------------------------------------------------------------- #
def fetch_focal_mechanism(detail_url: str):
    """
    Return (kind, fm, meta) where:
      kind == 'mt' -> fm = [Mrr, Mtt, Mpp, Mrt, Mrp, Mtp]  (N*m, RTP convention)
      kind == 'np' -> fm = [strike, dip, rake]             (degrees)
      kind is None -> no mechanism available
    meta is a dict with whatever derived magnitude / source info is present.
    """
    if not detail_url:
        return None, None, {}
    try:
        detail = requests.get(detail_url, headers=HEADERS, timeout=60).json()
    except Exception as exc:
        print(f"[warn] could not fetch event detail: {exc}", file=sys.stderr)
        return None, None, {}

    products = detail.get("properties", {}).get("products", {})

    def fnum(d, k):
        v = d.get(k)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Prefer a full moment tensor.
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

    # Fall back to a nodal plane from a focal-mechanism product.
    for fm in products.get("focal-mechanism", []):
        p = fm.get("properties", {})
        s = fnum(p, "nodal-plane-1-strike")
        d = fnum(p, "nodal-plane-1-dip")
        r = fnum(p, "nodal-plane-1-rake")
        if None not in (s, d, r):
            return "np", [s, d, r], {"source": p.get("eventsource")}

    return None, None, {}


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #
def _terrain_tiles(provider: str):
    import cartopy.io.img_tiles as cimgt

    class _Esri(cimgt.GoogleTiles):
        SERVICE = None  # set per subclass

        def _image_url(self, tile):
            x, y, z = tile
            return ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                    f"{self.SERVICE}/MapServer/tile/{z}/{y}/{x}")

    class ShadedRelief(_Esri):
        SERVICE = "World_Shaded_Relief"

    class WorldTerrain(_Esri):
        SERVICE = "World_Terrain_Base"

    class OpenTopo(cimgt.GoogleTiles):
        def _image_url(self, tile):
            x, y, z = tile
            return f"https://tile.opentopomap.org/{z}/{x}/{y}.png"

    return {
        "shaded-relief": ShadedRelief,
        "world-terrain": WorldTerrain,
        "opentopomap": OpenTopo,
    }[provider]()


def _tile_zoom(width_deg: float) -> int:
    z = int(round(math.log2(360.0 / max(width_deg, 1e-3)))) + 1
    return max(5, min(12, z))


def mag_to_size(m):
    # Exponential emphasis; area in points^2 for scatter.
    return 6.0 * np.power(2.0, np.clip(m, 0.0, 8.0))


def make_plot(mainshock: Event, events: list[Event], radius_km: float,
              mech_kind, fm, mech_meta, provider: str, out_path: str):
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cartopy.geodesic as cgeo
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize, TwoSlopeNorm
    from matplotlib.lines import Line2D

    pc = ccrs.PlateCarree()
    merc = ccrs.GOOGLE_MERCATOR

    # Extent from the buffer radius (with margin), in degrees.
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(mainshock.lat)))
    margin = 1.15
    extent = [
        mainshock.lon - dlon * margin, mainshock.lon + dlon * margin,
        mainshock.lat - dlat * margin, mainshock.lat + dlat * margin,
    ]

    fig = plt.figure(figsize=(11, 10))
    ax = plt.axes(projection=merc)
    ax.set_extent(extent, crs=pc)

    # Terrain basemap (graceful fallback to Natural Earth features).
    try:
        tiles = _terrain_tiles(provider)
        ax.add_image(tiles, _tile_zoom(extent[1] - extent[0]), interpolation="bilinear")
    except Exception as exc:
        print(f"[warn] terrain tiles unavailable ({exc}); using plain features.",
              file=sys.stderr)
        ax.add_feature(cfeature.LAND, facecolor="#e8e4dc")
        ax.add_feature(cfeature.OCEAN, facecolor="#cfe2f3")
        ax.add_feature(cfeature.COASTLINE, linewidth=0.6)
        ax.add_feature(cfeature.STATES, linewidth=0.4, edgecolor="0.4")

    # Event arrays.
    t0 = mainshock.time
    lons = np.array([e.lon for e in events])
    lats = np.array([e.lat for e in events])
    mags = np.array([e.mag for e in events])
    days = np.array([(e.time - t0).total_seconds() / 86400.0 for e in events])

    if days.min() < 0 < days.max():
        norm = TwoSlopeNorm(vmin=days.min(), vcenter=0.0, vmax=days.max())
        cmap = "coolwarm"
    else:
        norm = Normalize(vmin=days.min(), vmax=days.max())
        cmap = "viridis"

    sc = ax.scatter(
        lons, lats, transform=pc,
        s=mag_to_size(mags), c=days, cmap=cmap, norm=norm,
        edgecolor="k", linewidth=0.3, alpha=0.85, zorder=5,
    )
    cb = fig.colorbar(sc, ax=ax, shrink=0.7, pad=0.02)
    cb.set_label("Days relative to mainshock")

    # Buffer circle (true geodesic).
    circle = np.asarray(
        cgeo.Geodesic().circle(mainshock.lon, mainshock.lat, radius_km * 1000.0,
                               n_samples=181)
    )
    ax.plot(circle[:, 0], circle[:, 1], transform=pc,
            color="0.2", linewidth=1.2, linestyle="--", zorder=4,
            label=f"{radius_km:.0f} km buffer")

    # Moment tensor beachball (or a star fallback).
    mx, my = merc.transform_point(mainshock.lon, mainshock.lat, pc)
    x0, x1, _, _ = ax.get_extent(crs=merc)
    bb_width = 0.10 * (x1 - x0)
    placed_beachball = False
    if mech_kind in ("mt", "np") and fm is not None:
        try:
            from obspy.imaging.beachball import beach

            bball = beach(
                fm, xy=(mx, my), width=bb_width,
                facecolor="firebrick", edgecolor="k", linewidth=0.8,
                bgcolor="white", zorder=10,
            )
            ax.add_collection(bball)
            placed_beachball = True
        except Exception as exc:
            print(f"[warn] beachball render failed ({exc}); using star marker.",
                  file=sys.stderr)
    if not placed_beachball:
        ax.scatter([mainshock.lon], [mainshock.lat], transform=pc,
                   marker="*", s=600, facecolor="firebrick",
                   edgecolor="k", linewidth=1.0, zorder=10)

    # Magnitude legend.
    handles = [
        Line2D([0], [0], marker="o", linestyle="", markerfacecolor="0.6",
               markeredgecolor="k", markersize=math.sqrt(mag_to_size(m)),
               label=f"M {m}")
        for m in (2, 3, 4, 5)
    ]
    handles.append(Line2D([0], [0], color="0.2", linestyle="--",
                          label=f"{radius_km:.0f} km buffer"))
    leg = ax.legend(handles=handles, loc="lower left", title="Magnitude",
                    framealpha=0.9, fontsize=8)
    leg.get_title().set_fontsize(9)

    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="0.5", alpha=0.5)
    gl.top_labels = gl.right_labels = False

    mw = mech_meta.get("mw")
    mech_str = ""
    if placed_beachball:
        if mech_kind == "mt":
            mech_str = f"  |  MT{f' (Mw {mw:.1f})' if mw else ''}"
        else:
            mech_str = "  |  focal mechanism (nodal plane)"
    ax.set_title(
        f"Mainshock: {mainshock.mag:.1f} {mainshock.magtype} — {mainshock.place}\n"
        f"{mainshock.time:%Y-%m-%d %H:%M UTC}  |  depth {mainshock.depth_km:.1f} km"
        f"  |  {len(events)} events within {radius_km:.0f} km{mech_str}",
        fontsize=11,
    )

    fig.tight_layout()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    print(f"[ok] wrote {out_path}")
    return out_path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60,
                    help="Trailing window in days (default 60 ~ 2 months).")
    ap.add_argument("--ca-buffer-km", type=float, default=100.0,
                    help="Buffer around California for the regional search.")
    ap.add_argument("--regional-min-mag", type=float, default=3.0,
                    help="Min magnitude when hunting for the mainshock.")
    ap.add_argument("--local-min-mag", type=float, default=1.0,
                    help="Min magnitude for the surrounding sequence.")
    ap.add_argument("--buffer-factor", type=float, default=1.5,
                    help="Multiplier on Wells & Coppersmith rupture length.")
    ap.add_argument("--min-buffer-km", type=float, default=20.0)
    ap.add_argument("--max-buffer-km", type=float, default=300.0)
    ap.add_argument("--provider", default="shaded-relief",
                    choices=["shaded-relief", "world-terrain", "opentopomap"])
    ap.add_argument("--out", default="largest_ca_earthquake.png")
    args = ap.parse_args(argv)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    print(f"Window: {start:%Y-%m-%d} .. {end:%Y-%m-%d} UTC")

    # 1-2) Regional search within 100 km of CA.
    _, prepared, bbox, to3310 = california_buffer(args.ca_buffer_km)
    print(f"Search bbox (buffered CA): {tuple(round(b, 3) for b in bbox)}")
    candidates = query_box(start, end, bbox, args.regional_min_mag)
    inside = filter_inside(candidates, prepared, to3310)
    print(f"Candidates in bbox: {len(candidates)}; inside CA+buffer: {len(inside)}")
    if not inside:
        sys.exit("No events found inside the CA buffer for this window/threshold.")

    # 3) Mainshock.
    mainshock = max(inside, key=lambda e: e.mag)
    print(f"\nMainshock: {mainshock.mag:.1f} {mainshock.magtype} "
          f"{mainshock.place} ({mainshock.id})")
    print(f"  {mainshock.time:%Y-%m-%d %H:%M UTC}  "
          f"({mainshock.lat:.3f}, {mainshock.lon:.3f})  "
          f"depth {mainshock.depth_km:.1f} km")

    # 4) Magnitude-dependent buffer + sequence.
    radius_km = magnitude_buffer_km(
        mainshock.mag, factor=args.buffer_factor,
        min_km=args.min_buffer_km, max_km=args.max_buffer_km,
    )
    print(f"\nMagnitude-dependent buffer: {radius_km:.1f} km "
          f"(W&C 1994 RLD x {args.buffer_factor})")
    sequence = query_circle(start, end, mainshock.lat, mainshock.lon,
                            radius_km, args.local_min_mag)
    print(f"Events within buffer: {len(sequence)}")

    # 5) Moment tensor.
    kind, fm, meta = fetch_focal_mechanism(mainshock.detail_url)
    if kind == "mt":
        print(f"Moment tensor found (source: {meta.get('source')}).")
    elif kind == "np":
        print(f"Focal mechanism (nodal plane) found: {fm}")
    else:
        print("No moment tensor / focal mechanism available for the mainshock.")

    # 6) Plot.
    make_plot(mainshock, sequence, radius_km, kind, fm, meta,
              args.provider, args.out)


if __name__ == "__main__":
    main()
