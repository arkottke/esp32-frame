"""
Hourly weather forecast plugin using Open-Meteo (no API key required).

Renders a 4-panel graphical forecast similar to the NWS hourly graphical page:
  - Temperature (line + fill)
  - Cloud cover (area)
  - Precipitation probability (bar)
  - Wind speed (line)

config = {
    "type": "weather",
    "lat": 37.895,
    "lon": -122.295,
    "location_name": "Point Richmond, CA",
    "forecast_hours": 48,
    "temperature_unit": "fahrenheit"   # or "celsius"
}
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

import httpx
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
import numpy as np
from PIL import Image

from plugins.base import Plugin

_TEMP_UNIT_LABEL = {"fahrenheit": "°F", "celsius": "°C"}


class WeatherPlugin(Plugin):
    plugin_type = "weather"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.lat: float = config.get("lat", 0.0)
        self.lon: float = config.get("lon", 0.0)
        self.location_name: str = config.get("location_name", "")
        self.forecast_hours: int = min(max(config.get("forecast_hours", 48), 12), 168)
        self.temp_unit: str = config.get("temperature_unit", "fahrenheit")

    def render(self) -> Image.Image:
        try:
            data = self._fetch()
            return self._draw(data)
        except Exception as exc:
            return self._error_image(str(exc))

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch(self) -> dict:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.lat}&longitude={self.lon}"
            "&hourly=temperature_2m,precipitation_probability,"
            "windspeed_10m,cloudcover"
            f"&temperature_unit={self.temp_unit}"
            "&wind_speed_unit=mph"
            f"&forecast_hours={self.forecast_hours}"
            "&timezone=auto"
        )
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Chart rendering
    # ------------------------------------------------------------------

    def _draw(self, data: dict) -> Image.Image:
        hourly = data.get("hourly", {})
        times  = [datetime.fromisoformat(t) for t in hourly.get("time", [])]
        temp   = _as_floats(hourly.get("temperature_2m", []))
        pop    = _as_floats(hourly.get("precipitation_probability", []))
        wind   = _as_floats(hourly.get("windspeed_10m", []))
        cloud  = _as_floats(hourly.get("cloudcover", []))

        n = min(len(times), len(temp), len(pop), len(wind), len(cloud))
        times, temp, pop, wind, cloud = (
            times[:n], temp[:n], pop[:n], wind[:n], cloud[:n]
        )
        xs = np.arange(n)

        fig = Figure(figsize=(12, 16), dpi=100)
        fig.patch.set_facecolor("white")

        gs = GridSpec(4, 1, figure=fig,
                      height_ratios=[2.5, 1.5, 1.5, 1.5],
                      hspace=0.08,
                      left=0.10, right=0.97,
                      top=0.92, bottom=0.07)

        ax_temp  = fig.add_subplot(gs[0])
        ax_cloud = fig.add_subplot(gs[1], sharex=ax_temp)
        ax_pop   = fig.add_subplot(gs[2], sharex=ax_temp)
        ax_wind  = fig.add_subplot(gs[3], sharex=ax_temp)
        axes = [ax_temp, ax_cloud, ax_pop, ax_wind]

        # ---- day/night shading on all panels -------------------------
        for ax in axes:
            _shade_day_night(ax, times, xs)

        # ---- temperature --------------------------------------------
        ax_temp.plot(xs, temp, color="#cc2200", linewidth=2.5, zorder=3)
        ax_temp.fill_between(xs, temp, alpha=0.25, color="#ff6633", zorder=2)
        ax_temp.set_ylabel(_TEMP_UNIT_LABEL.get(self.temp_unit, "°F"), fontsize=11)
        ax_temp.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_temp, ylabel_right=False)
        # annotate current value
        if temp:
            ax_temp.annotate(
                f"{temp[0]:.0f}{_TEMP_UNIT_LABEL.get(self.temp_unit, '°')}",
                xy=(0, temp[0]), xytext=(6, 0), textcoords="offset points",
                fontsize=10, color="#cc2200", va="center",
            )

        # ---- cloud cover --------------------------------------------
        ax_cloud.fill_between(xs, cloud, alpha=0.55, color="#888888", zorder=2)
        ax_cloud.plot(xs, cloud, color="#555555", linewidth=1.2, zorder=3)
        ax_cloud.set_ylim(0, 100)
        ax_cloud.set_ylabel("Cloud\n(%)", fontsize=10)
        ax_cloud.yaxis.set_label_coords(-0.07, 0.5)
        ax_cloud.set_yticks([0, 50, 100])
        _style_ax(ax_cloud)

        # ---- precipitation probability ------------------------------
        bar_color = np.where(np.array(pop) >= 50, "#1144cc", "#7799ee")
        ax_pop.bar(xs, pop, color=bar_color, width=0.85, zorder=3)
        ax_pop.set_ylim(0, 100)
        ax_pop.set_ylabel("Precip\n(%)", fontsize=10)
        ax_pop.yaxis.set_label_coords(-0.07, 0.5)
        ax_pop.set_yticks([0, 50, 100])
        _style_ax(ax_pop)

        # ---- wind speed ---------------------------------------------
        ax_wind.plot(xs, wind, color="#006633", linewidth=2, zorder=3)
        ax_wind.fill_between(xs, wind, alpha=0.20, color="#00aa55", zorder=2)
        ax_wind.set_ylim(bottom=0)
        ax_wind.set_ylabel("Wind\n(mph)", fontsize=10)
        ax_wind.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_wind)

        # ---- shared x-axis: 6-hour ticks + day labels ---------------
        tick_positions, tick_labels = _build_x_ticks(times, xs)
        ax_wind.set_xticks(tick_positions)
        ax_wind.set_xticklabels(tick_labels, fontsize=9, rotation=0)
        ax_wind.set_xlim(0, n - 1)

        # Hide x tick labels on upper panels
        for ax in [ax_temp, ax_cloud, ax_pop]:
            ax.tick_params(labelbottom=False)

        # Day separator lines + day name labels at top
        _draw_day_markers(axes, ax_temp, times, xs, fig)

        # ---- title --------------------------------------------------
        title = self.location_name or f"{self.lat:.3f}, {self.lon:.3f}"
        if times:
            date_range = (
                f"{times[0].strftime('%a %b %-d')} – "
                f"{times[-1].strftime('%a %b %-d, %Y')}"
            )
            title += f"  ·  {date_range}"
        fig.suptitle(title, fontsize=14, fontweight="bold", y=0.975)

        # ---- panel labels (right side) ------------------------------
        for ax, label in zip(axes, ["Temperature", "Cloud Cover",
                                     "Precip Prob.", "Wind Speed"]):
            ax.text(1.0, 1.0, label, transform=ax.transAxes,
                    fontsize=10, ha="right", va="top",
                    color="#333333", style="italic")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor="white")
        buf.seek(0)
        return Image.open(buf).convert("RGB")

    # ------------------------------------------------------------------
    # Error fallback
    # ------------------------------------------------------------------

    def _error_image(self, msg: str) -> Image.Image:
        import textwrap
        from PIL import ImageDraw, ImageFont
        img = Image.new("RGB", (1200, 1600), (255, 245, 245))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        except OSError:
            font = ImageFont.load_default()
        draw.text((60, 60), "Weather fetch failed:", font=font, fill=(180, 0, 0))
        for i, line in enumerate(textwrap.wrap(msg, 40)):
            draw.text((60, 140 + i * 60), line, font=font, fill=(80, 0, 0))
        return img


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _as_floats(lst: list) -> list[float]:
    return [float(v) if v is not None else float("nan") for v in lst]


def _style_ax(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cccccc")
    ax.spines["bottom"].set_color("#cccccc")
    ax.tick_params(axis="y", labelsize=9, colors="#555555")
    ax.yaxis.grid(True, color="#eeeeee", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.set_facecolor("white")


def _shade_day_night(ax, times: list[datetime], xs) -> None:
    """Light yellow fill during daytime hours (6 am – 8 pm)."""
    if not times:
        return
    in_day = False
    start_x = None
    for i, (t, x) in enumerate(zip(times, xs)):
        is_day = 6 <= t.hour < 20
        if is_day and not in_day:
            start_x = x
            in_day = True
        elif not is_day and in_day:
            ax.axvspan(start_x - 0.5, x - 0.5,
                       color="#fffde7", alpha=0.6, zorder=0)
            in_day = False
    if in_day and start_x is not None:
        ax.axvspan(start_x - 0.5, xs[-1] + 0.5,
                   color="#fffde7", alpha=0.6, zorder=0)


def _build_x_ticks(
    times: list[datetime], xs
) -> tuple[list[float], list[str]]:
    """Return (positions, labels) for 6-hour ticks on the bottom axis."""
    positions, labels = [], []
    for i, t in enumerate(times):
        if t.hour % 6 == 0:
            positions.append(xs[i])
            if t.hour == 0:
                labels.append("")          # midnight — label handled by day marker
            elif t.hour == 6:
                labels.append("6am")
            elif t.hour == 12:
                labels.append("12pm")
            elif t.hour == 18:
                labels.append("6pm")
    return positions, labels


def _draw_day_markers(
    axes, ax_top, times: list[datetime], xs, fig
) -> None:
    """Dashed vertical line at each midnight + day name above top panel."""
    seen: set[str] = set()
    for i, t in enumerate(times):
        if t.hour == 0:
            x = xs[i]
            for ax in axes:
                ax.axvline(x - 0.5, color="#aaaaaa", linewidth=0.8,
                           linestyle="--", zorder=1)
            day_str = t.strftime("%a")
            if day_str not in seen:
                seen.add(day_str)
                ax_top.text(
                    x, 1.04, day_str,
                    transform=ax_top.get_xaxis_transform(),
                    ha="center", va="bottom",
                    fontsize=11, fontweight="bold", color="#333333",
                )
