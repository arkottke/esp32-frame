from __future__ import annotations

from typing import Any

from PIL import Image

from plugins.base import Plugin


class PlaylistPlugin(Plugin):
    """
    Cycle through a list of plugin configs on each render call.

    config = {
        "type": "playlist",
        "items": [
            {"type": "static", "filename": "photo.jpg"},
            {"type": "weather", "lat": 37.8, "lon": -122.4, ...},
        ],
        "current_index": 0   # maintained by the server between renders
    }
    """

    plugin_type = "playlist"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.items: list[dict[str, Any]] = config.get("items", [])
        # current_index is stored in the mutable config dict so the server
        # can persist it back to config.json after each render.
        if "current_index" not in config:
            config["current_index"] = 0

    def render(self) -> Image.Image:
        if not self.items:
            return self.blank_white()

        idx = self.config["current_index"] % len(self.items)
        item_config = self.items[idx]

        # Advance for next call — caller must persist config back to disk
        self.config["current_index"] = (idx + 1) % len(self.items)

        try:
            plugin = Plugin.from_config(item_config)
            return plugin.render()
        except Exception:
            return self.blank_white()
