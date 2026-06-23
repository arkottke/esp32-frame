"""
Hourly weather forecast plugin using Open-Meteo (no API key required).

Renders a 4-panel graphical forecast similar to the NWS hourly graphical page:
  - Temperature + dew point (line + fill)
  - Wind speed + gusts + direction barbs (area + line + barbs)
  - Relative humidity / precip probability / sky cover (line + bar)
  - Precipitation amount (bar)

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

# ACeP 6-color palette — use these exact values to avoid dithering
C_BLACK  = "#000000"
C_WHITE  = "#ffffff"
C_YELLOW = "#ffff00"
C_RED    = "#ff0000"
C_BLUE   = "#0000ff"
C_GREEN  = "#00ff00"

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
        precip_unit = "inch" if self.temp_unit == "fahrenheit" else "mm"
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.lat}&longitude={self.lon}"
            "&hourly=temperature_2m,dewpoint_2m,precipitation_probability,"
            "windspeed_10m,windgusts_10m,winddirection_10m,"
            "cloudcover,relativehumidity_2m,precipitation"
            f"&temperature_unit={self.temp_unit}"
            "&wind_speed_unit=mph"
            f"&precipitation_unit={precip_unit}"
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
        dew    = _as_floats(hourly.get("dewpoint_2m", []))
        pop    = _as_floats(hourly.get("precipitation_probability", []))
        wind   = _as_floats(hourly.get("windspeed_10m", []))
        gusts  = _as_floats(hourly.get("windgusts_10m", []))
        wdir   = _as_floats(hourly.get("winddirection_10m", []))
        cloud  = _as_floats(hourly.get("cloudcover", []))
        rh     = _as_floats(hourly.get("relativehumidity_2m", []))
        precip = _as_floats(hourly.get("precipitation", []))

        n = min(len(times), len(temp), len(dew), len(pop), len(wind),
                len(gusts), len(wdir), len(cloud), len(rh), len(precip))
        times, temp, dew, pop, wind, gusts, wdir, cloud, rh, precip = (
            times[:n], temp[:n], dew[:n], pop[:n], wind[:n],
            gusts[:n], wdir[:n], cloud[:n], rh[:n], precip[:n]
        )
        xs = np.arange(n)

        fig = Figure(figsize=(12, 16), dpi=100)
        fig.patch.set_facecolor("white")

        # GridSpec constants — referenced again below for barb aspect correction
        _LEFT, _RIGHT, _TOP, _BOT = 0.10, 0.97, 0.92, 0.07
        _RATIOS = [2.5, 1.5, 1.5, 1.5]
        gs = GridSpec(4, 1, figure=fig,
                      height_ratios=_RATIOS,
                      hspace=0.08,
                      left=_LEFT, right=_RIGHT,
                      top=_TOP, bottom=_BOT)

        ax_temp   = fig.add_subplot(gs[0])
        ax_wind   = fig.add_subplot(gs[1], sharex=ax_temp)
        ax_multi  = fig.add_subplot(gs[2], sharex=ax_temp)
        ax_precip = fig.add_subplot(gs[3], sharex=ax_temp)
        axes = [ax_temp, ax_wind, ax_multi, ax_precip]

        # ---- temperature + dew point --------------------------------
        ax_temp.plot(xs, temp, color=C_RED, linewidth=3, zorder=3)
        ax_temp.plot(xs, dew, color=C_BLUE, linewidth=2, zorder=3)
        ax_temp.set_ylabel(_TEMP_UNIT_LABEL.get(self.temp_unit, "°F"), fontsize=14)
        ax_temp.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_temp)
        if temp:
            ax_temp.annotate(
                f"{temp[0]:.0f}{_TEMP_UNIT_LABEL.get(self.temp_unit, '°')}",
                xy=(0, temp[0]), xytext=(6, 0), textcoords="offset points",
                fontsize=13, color=C_RED, va="center",
            )

        # ---- wind speed + gusts + direction barbs -------------------
        ax_wind.plot(xs, wind, color=C_GREEN, linewidth=3, zorder=3)
        ax_wind.plot(xs, gusts, color=C_RED, linewidth=2, zorder=2)
        # Fix ylim before computing aspect correction for barbs
        valid_top = [g for g in gusts if not np.isnan(g)]
        y_max_wind = (max(valid_top) * 1.1) if valid_top else 20.0
        ax_wind.set_ylim(0, y_max_wind)
        ax_wind.set_ylabel("Wind\n(mph)", fontsize=13)
        ax_wind.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_wind)
        # Aspect-ratio correction: barbs are drawn in data space, so their
        # apparent screen angle depends on x_scale vs y_scale.  Rescale u so
        # that a compass angle looks correct on the rendered image.
        _grid_h_in = (_TOP - _BOT) * 16
        _grid_w_in = (_RIGHT - _LEFT) * 12
        _wind_h_in = (_RATIOS[1] / sum(_RATIOS)) * _grid_h_in
        _x_scl = _grid_w_in / max(n - 1, 1)   # inches per hour
        _y_scl = _wind_h_in / y_max_wind        # inches per mph
        _r = _y_scl / _x_scl
        dir_rad = np.radians(np.array(wdir))
        # Met convention: u/v point FROM the wind source (barb tip toward source)
        u_dir = np.sin(dir_rad)
        v_dir = np.cos(dir_rad)
        u_corr = u_dir * _r
        norm = np.hypot(u_corr, v_dir)
        norm = np.where(norm < 1e-10, 1e-10, norm)
        spd = np.array(gusts)
        u_b = u_corr / norm * spd
        v_b = v_dir  / norm * spd
        ax_wind.barbs(xs, np.array(wind), u_b, v_b,
                      length=7, pivot='tip',
                      barb_increments=dict(half=5, full=10, flag=50),
                      color=C_BLACK, linewidth=0.8)

        # ---- RH / precip prob / sky cover ---------------------------
        ax_multi.bar(xs, pop, color=C_BLUE, width=0.85, zorder=2)
        ax_multi.plot(xs, rh, color=C_RED, linewidth=2, zorder=3)
        ax_multi.plot(xs, cloud, color=C_BLACK, linewidth=1.5,
                      linestyle="--", zorder=3)
        ax_multi.set_ylim(0, 100)
        ax_multi.set_ylabel("%", fontsize=13)
        ax_multi.yaxis.set_label_coords(-0.07, 0.5)
        ax_multi.set_yticks([0, 50, 100])
        _style_ax(ax_multi)

        # ---- precipitation amount -----------------------------------
        precip_unit_label = "in" if self.temp_unit == "fahrenheit" else "mm"
        ax_precip.bar(xs, precip, color=C_BLUE, width=0.85, zorder=3)
        ax_precip.set_ylim(bottom=0)
        ax_precip.set_ylabel(f"Precip\n({precip_unit_label})", fontsize=13)
        ax_precip.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_precip)

        # ---- shared x-axis: 6-hour ticks + day labels ---------------
        tick_positions, tick_labels = _build_x_ticks(times, xs)
        ax_precip.set_xticks(tick_positions)
        ax_precip.set_xticklabels(tick_labels, fontsize=13, rotation=0)
        ax_precip.set_xlim(0, n - 1)

        for ax in [ax_temp, ax_wind, ax_multi]:
            ax.tick_params(labelbottom=False)

        # Night shading (light grey) + day separator lines + day name labels
        for ax in axes:
            _shade_day_night(ax, times, xs)
        _draw_day_markers(axes, ax_temp, times, xs, fig)

        # ---- title --------------------------------------------------
        title = self.location_name or f"{self.lat:.3f}, {self.lon:.3f}"
        if times:
            date_range = (
                f"{times[0].strftime('%a %b %-d')} – "
                f"{times[-1].strftime('%a %b %-d, %Y')}"
            )
            title += f"  ·  {date_range}"
        fig.suptitle(title, fontsize=18, fontweight="bold", y=0.975)

        # ---- panel labels (right side) ------------------------------
        for ax, label in zip(axes, ["Temperature", "Wind",
                                     "RH / PoP / Sky", "Precipitation"]):
            ax.text(1.0, 1.0, label, transform=ax.transAxes,
                    fontsize=13, ha="right", va="top",
                    color=C_BLACK, style="italic")

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
    ax.spines["left"].set_color(C_BLACK)
    ax.spines["bottom"].set_color(C_BLACK)
    ax.tick_params(axis="y", labelsize=13, colors=C_BLACK)
    ax.yaxis.grid(False)
    ax.set_facecolor(C_WHITE)


def _shade_day_night(ax, times: list[datetime], xs) -> None:
    """Light gray fill during daytime hours (6 am – 8 pm)."""
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
                       color="#d3d3d3", alpha=0.6, zorder=0)
            in_day = False
    if in_day and start_x is not None:
        ax.axvspan(start_x - 0.5, xs[-1] + 0.5,
                   color="#d3d3d3", alpha=0.6, zorder=0)


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
                ax.axvline(x - 0.5, color=C_BLACK, linewidth=1,
                           linestyle="--", zorder=4)
            day_str = t.strftime("%a")
            if day_str not in seen:
                seen.add(day_str)
                ax_top.text(
                    x, 1.04, day_str,
                    transform=ax_top.get_xaxis_transform(),
                    ha="center", va="bottom",
                    fontsize=14, fontweight="bold", color=C_BLACK,
                )
