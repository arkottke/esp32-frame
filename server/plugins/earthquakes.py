"""
Earthquake map plugin using the USGS GeoJSON feed (no API key required).

Plots recent earthquake epicenters on a world map using Matplotlib.

config = {
    "type": "earthquakes",
    "days": 7,          # 1, 7, or 30
    "min_magnitude": 4.5
}
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from PIL import Image

from plugins.base import Plugin

# USGS GeoJSON feeds: significant / 4.5+ / 2.5+ over 1, 7, 30 days
_FEED_BASE = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary"

def _feed_url(days: int, min_mag: float) -> str:
    period = {1: "day", 7: "week", 30: "month"}.get(days, "week")
    if min_mag >= 4.5:
        level = "4.5"
    elif min_mag >= 2.5:
        level = "2.5"
    else:
        level = "all"
    return f"{_FEED_BASE}/{level}_{period}.geojson"


class EarthquakePlugin(Plugin):
    plugin_type = "earthquakes"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.days: int = config.get("days", 7)
        self.min_magnitude: float = config.get("min_magnitude", 4.5)

    def render(self) -> Image.Image:
        try:
            features = self._fetch()
            return self._draw(features)
        except Exception as exc:
            return self._error_image(str(exc))

    def _fetch(self) -> list[dict]:
        url = _feed_url(self.days, self.min_magnitude)
        resp = httpx.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("features", [])

    def _draw(self, features: list[dict]) -> Image.Image:
        # Portrait 1200x1600 at 100 dpi → 12x16 inches
        fig, ax = plt.subplots(figsize=(12, 16), dpi=100)
        fig.patch.set_facecolor("#0d1b2a")
        ax.set_facecolor("#0d1b2a")

        # Draw simple coastline using Natural Earth low-res data via matplotlib
        # (no basemap/cartopy dependency — just draw a world outline rectangle)
        ax.set_xlim(-180, 180)
        ax.set_ylim(-90, 90)
        ax.set_aspect("equal")

        # Grid lines
        for lon in range(-180, 181, 30):
            ax.axvline(lon, color="#1a3a5c", linewidth=0.5, alpha=0.6)
        for lat in range(-90, 91, 30):
            ax.axhline(lat, color="#1a3a5c", linewidth=0.5, alpha=0.6)

        # Tectonic plate outlines would require data files; skip for now.
        # Plot earthquake dots scaled by magnitude
        lons, lats, mags = [], [], []
        for f in features:
            coords = f.get("geometry", {}).get("coordinates", [])
            props  = f.get("properties", {})
            if len(coords) >= 2:
                lon, lat = coords[0], coords[1]
                mag = props.get("mag") or 0
                if mag >= self.min_magnitude:
                    lons.append(lon)
                    lats.append(lat)
                    mags.append(mag)

        if lons:
            sizes = [max(10, (m - self.min_magnitude + 1) ** 2.5 * 8) for m in mags]
            colors = [_mag_color(m) for m in mags]
            ax.scatter(lons, lats, s=sizes, c=colors, alpha=0.75, linewidths=0.3,
                       edgecolors="white", zorder=3)

        # Labels
        period_label = {1: "24 hours", 7: "7 days", 30: "30 days"}.get(self.days, f"{self.days} days")
        ax.set_title(
            f"Earthquakes M≥{self.min_magnitude} — last {period_label}  ({len(lons)} events)",
            color="white", fontsize=16, pad=10
        )
        ax.tick_params(colors="gray", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#1a3a5c")

        # Legend
        legend_mags = [5.0, 6.0, 7.0, 8.0]
        patches = [
            mpatches.Patch(color=_mag_color(m), label=f"M{m:.0f}+")
            for m in legend_mags if m >= self.min_magnitude
        ]
        ax.legend(handles=patches, loc="lower left", fontsize=10,
                  facecolor="#0d1b2a", edgecolor="gray", labelcolor="white")

        plt.tight_layout(pad=0.5)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return Image.open(buf).convert("RGB")

    def _error_image(self, msg: str) -> Image.Image:
        from PIL import ImageDraw, ImageFont
        import textwrap
        img = Image.new("RGB", (1200, 1600), (20, 20, 40))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        except OSError:
            font = ImageFont.load_default()
        draw.text((60, 60), "Earthquake fetch failed:", font=font, fill=(255, 100, 80))
        for i, line in enumerate(textwrap.wrap(msg, 40)):
            draw.text((60, 140 + i * 60), line, font=font, fill=(200, 200, 200))
        return img


def _mag_color(mag: float) -> str:
    if mag >= 8.0:
        return "#ff2200"
    if mag >= 7.0:
        return "#ff7700"
    if mag >= 6.0:
        return "#ffcc00"
    if mag >= 5.0:
        return "#88ff44"
    return "#44aaff"
