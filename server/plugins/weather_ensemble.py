"""
Ensemble weather forecast plugin using Open-Meteo ensemble API (no API key).

Renders a 4-panel forecast showing ensemble spread (uncertainty):
  1. Temperature (red)  — all members + mean
  2. Wind speed (green) — all members + mean
  3. Configurable variable (yellow) — wind_gusts_10m | cloud_cover | surface_pressure
  4. Precipitation (blue) — member lines + mean bars

config = {
    "type": "weather_ensemble",
    "lat": 37.895,
    "lon": -122.295,
    "location_name": "Point Richmond, CA",
    "days": 7,
    "temperature_unit": "fahrenheit",    # or "celsius"
    "model": "ecmwf_ifs025",              # or "gfs_seamless", "icon_seamless"
    "yellow_variable": "wind_gusts_10m"  # or "cloud_cover", "surface_pressure"
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

C_BLACK  = "#000000"
C_WHITE  = "#ffffff"
C_YELLOW = "#ffff00"
C_RED    = "#ff0000"
C_BLUE   = "#0000ff"
C_GREEN  = "#00ff00"

_TEMP_UNIT_LABEL = {"fahrenheit": "°F", "celsius": "°C"}

_YELLOW_VAR_META: dict[str, dict] = {
    "wind_gusts_10m":   {"label": "Gusts"},
    "cloud_cover":      {"label": "Cloud Cover", "ylim": (0, 100)},
    "surface_pressure": {"label": "Pressure"},
}

_MODEL_LABEL: dict[str, str] = {
    "ecmwf_ifs025":  "ECMWF IFS",
    "gfs_seamless":  "GFS",
    "icon_seamless": "ICON",
}

# Remap stale or wrong model IDs transparently
_MODEL_ALIAS: dict[str, str] = {
    "ecmwf_ifs04": "ecmwf_ifs025",
}


class WeatherEnsemblePlugin(Plugin):
    plugin_type = "weather_ensemble"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.lat: float = float(config.get("lat", 0.0))
        self.lon: float = float(config.get("lon", 0.0))
        self.location_name: str = config.get("location_name", "")
        self.days: int = min(max(int(config.get("days", 7)), 1), 10)
        self.temp_unit: str = config.get("temperature_unit", "fahrenheit")
        self.model: str = _MODEL_ALIAS.get(
            config.get("model", "ecmwf_ifs025"),
            config.get("model", "ecmwf_ifs025"),
        )
        self.yellow_var: str = config.get("yellow_variable", "wind_gusts_10m")
        if self.yellow_var not in _YELLOW_VAR_META:
            self.yellow_var = "wind_gusts_10m"
        self.wind_unit: str = "mph" if self.temp_unit == "fahrenheit" else "kmh"

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
        variables = [
            "temperature_2m",
            "wind_speed_10m",
            self.yellow_var,
            "precipitation",
        ]
        url = (
            "https://ensemble-api.open-meteo.com/v1/ensemble"
            f"?latitude={self.lat}&longitude={self.lon}"
            f"&models={self.model}"
            f"&hourly={','.join(variables)}"
            f"&temperature_unit={self.temp_unit}"
            f"&wind_speed_unit={self.wind_unit}"
            f"&precipitation_unit={precip_unit}"
            f"&forecast_days={self.days}"
            "&timezone=auto"
        )
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        hourly = raw.get("hourly", {})
        times = [datetime.fromisoformat(t) for t in hourly.get("time", [])]

        def _gather(var_name: str) -> list[list[float]]:
            prefix = f"{var_name}_member"
            keys = sorted(k for k in hourly if k.startswith(prefix))
            return [_as_floats(hourly[k]) for k in keys]

        return {
            "times":       times,
            "temperature": _gather("temperature_2m"),
            "wind":        _gather("wind_speed_10m"),
            "yellow":      _gather(self.yellow_var),
            "precip":      _gather("precipitation"),
        }

    # ------------------------------------------------------------------
    # Chart rendering
    # ------------------------------------------------------------------

    def _draw(self, data: dict) -> Image.Image:
        times = data["times"]
        n = len(times)
        if n == 0:
            return self._error_image("No data returned from API")
        xs = np.arange(n)

        fig = Figure(figsize=(12, 16), dpi=100)
        fig.patch.set_facecolor("white")

        _LEFT, _RIGHT, _TOP, _BOT = 0.10, 0.97, 0.92, 0.07
        gs = GridSpec(4, 1, figure=fig,
                      height_ratios=[2.5, 1.5, 1.5, 1.5],
                      hspace=0.08,
                      left=_LEFT, right=_RIGHT,
                      top=_TOP, bottom=_BOT)

        ax_temp   = fig.add_subplot(gs[0])
        ax_wind   = fig.add_subplot(gs[1], sharex=ax_temp)
        ax_yellow = fig.add_subplot(gs[2], sharex=ax_temp)
        ax_precip = fig.add_subplot(gs[3], sharex=ax_temp)
        axes = [ax_temp, ax_wind, ax_yellow, ax_precip]

        # ---- temperature (red) ----------------------------------------
        _plot_ensemble_band(ax_temp, xs, data["temperature"], C_RED)
        ax_temp.set_ylabel(_TEMP_UNIT_LABEL.get(self.temp_unit, "°F"), fontsize=14)
        ax_temp.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_temp)

        # ---- wind speed (green) ----------------------------------------
        _plot_ensemble_band(ax_wind, xs, data["wind"], C_GREEN)
        wind_label = "mph" if self.wind_unit == "mph" else "km/h"
        ax_wind.set_ylabel(f"Wind\n({wind_label})", fontsize=13)
        ax_wind.yaxis.set_label_coords(-0.07, 0.5)
        ax_wind.set_ylim(bottom=0)
        _style_ax(ax_wind)

        # ---- yellow variable -------------------------------------------
        _plot_ensemble_band(ax_yellow, xs, data["yellow"], C_YELLOW)
        ymeta = _YELLOW_VAR_META[self.yellow_var]
        if self.yellow_var == "wind_gusts_10m":
            ylabel = f"Gusts\n({wind_label})"
            ax_yellow.set_ylim(bottom=0)
        elif self.yellow_var == "cloud_cover":
            ylabel = "Cloud\n(%)"
            ax_yellow.set_ylim(0, 100)
        else:  # surface_pressure
            ylabel = "Pressure\n(hPa)"
        ax_yellow.set_ylabel(ylabel, fontsize=13)
        ax_yellow.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_yellow)

        # ---- precipitation (blue) — percentile band + mean bars ---------
        _plot_ensemble_band(ax_precip, xs, data["precip"], C_BLUE)
        mean_precip = _ensemble_mean(data["precip"])
        if mean_precip is not None:
            ax_precip.bar(xs, mean_precip, color=C_BLUE, width=0.6,
                          alpha=1.0, zorder=3)
        precip_label = "in" if self.temp_unit == "fahrenheit" else "mm"
        precip_ymax_min = 0.5 if self.temp_unit == "fahrenheit" else 13.0
        current_top = ax_precip.get_ylim()[1]
        ax_precip.set_ylim(bottom=0, top=max(current_top, precip_ymax_min))
        ax_precip.set_ylabel(f"Precip\n({precip_label})", fontsize=13)
        ax_precip.yaxis.set_label_coords(-0.07, 0.5)
        _style_ax(ax_precip)

        # ---- shared x-axis: 6-hour ticks + day labels ------------------
        tick_positions, tick_labels = _build_x_ticks(times, xs)
        ax_precip.set_xticks(tick_positions)
        ax_precip.set_xticklabels(tick_labels, fontsize=13, rotation=0)
        ax_precip.set_xlim(-0.5, n - 0.5)

        for ax in [ax_temp, ax_wind, ax_yellow]:
            ax.tick_params(labelbottom=False)

        _draw_day_markers(axes, ax_temp, times, xs, fig)

        # ---- panel labels (right side) ---------------------------------
        for ax, label in zip(axes, ["Temperature", "Wind Speed",
                                     ymeta["label"], "Precipitation"]):
            ax.text(1.0, 1.0, label, transform=ax.transAxes,
                    fontsize=13, ha="right", va="top",
                    color=C_BLACK, style="italic")

        # ---- title ------------------------------------------------------
        loc_str = self.location_name or f"{self.lat:.3f}, {self.lon:.3f}"
        model_str = _MODEL_LABEL.get(self.model, self.model)
        title = f"{loc_str}  ·  {model_str} Ensemble"
        if times:
            date_range = (
                f"{times[0].strftime('%a %b %-d')} – "
                f"{times[-1].strftime('%a %b %-d, %Y')}"
            )
            title += f"  ·  {date_range}"
        fig.suptitle(title, fontsize=16, fontweight="bold", y=0.975)

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
        draw.text((60, 60), "Ensemble fetch failed:", font=font, fill=(180, 0, 0))
        for i, line in enumerate(textwrap.wrap(msg, 40)):
            draw.text((60, 140 + i * 60), line, font=font, fill=(80, 0, 0))
        return img


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _plot_ensemble_band(ax, xs, members: list[list[float]], color: str) -> None:
    """Shaded percentile bands + mean line — readable after e-ink dithering."""
    if not members:
        return
    arr = np.array(members, dtype=float)
    p10 = np.nanpercentile(arr, 10, axis=0)
    p25 = np.nanpercentile(arr, 25, axis=0)
    p75 = np.nanpercentile(arr, 75, axis=0)
    p90 = np.nanpercentile(arr, 90, axis=0)
    mean = np.nanmean(arr, axis=0)
    # Outer band (10th–90th): sparse dither pattern on e-ink
    ax.fill_between(xs, p10, p90, color=color, alpha=0.25, zorder=1)
    # Inner band (25th–75th): denser dither pattern
    ax.fill_between(xs, p25, p75, color=color, alpha=0.45, zorder=2)
    # Mean: solid line
    ax.plot(xs, mean, color=color, linewidth=2.2, alpha=1.0, zorder=3)


def _ensemble_mean(members: list[list[float]]) -> list[float] | None:
    if not members:
        return None
    arr = np.array(members, dtype=float)
    return np.nanmean(arr, axis=0).tolist()


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
    positions, labels = [], []
    for i, t in enumerate(times):
        if t.hour % 6 == 0:
            positions.append(float(xs[i]))
            labels.append("24" if t.hour == 0 else str(t.hour))
    return positions, labels


def _draw_day_markers(
    axes, ax_top, times: list[datetime], xs, fig
) -> None:
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
