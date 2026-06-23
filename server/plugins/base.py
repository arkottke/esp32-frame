from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from PIL import Image


class Plugin(ABC):
    """
    Base class for all content plugins.

    Subclasses implement render() to return a 1200×1600 RGB PIL Image.
    The server calls render() on each image request, then dithers the result.
    """

    #: Short identifier used in config.json (e.g. "static", "weather")
    plugin_type: str = ""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def render(self) -> Image.Image:
        """Return a 1200×1600 RGB PIL Image."""

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "Plugin":
        """Instantiate the correct subclass based on config['type']."""
        from plugins.static import StaticPlugin
        from plugins.playlist import PlaylistPlugin
        from plugins.weather import WeatherPlugin
        from plugins.earthquakes import EarthquakePlugin
        from plugins.gallery import GalleryPlugin

        registry: dict[str, type[Plugin]] = {
            "static": StaticPlugin,
            "playlist": PlaylistPlugin,
            "weather": WeatherPlugin,
            "earthquakes": EarthquakePlugin,
            "gallery": GalleryPlugin,
        }
        plugin_type = config.get("type", "static")
        if plugin_type not in registry:
            raise ValueError(f"Unknown plugin type: {plugin_type!r}")
        return registry[plugin_type](config)

    @staticmethod
    def blank_white() -> Image.Image:
        return Image.new("RGB", (1200, 1600), (255, 255, 255))
