from __future__ import annotations

import os
from typing import Any

from PIL import Image

from plugins.base import Plugin

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads")


class StaticPlugin(Plugin):
    """Serve a single uploaded image."""

    plugin_type = "static"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        # config["filename"] is relative to uploads/
        self.filename: str = config.get("filename", "")

    def render(self) -> Image.Image:
        if not self.filename:
            return self.blank_white()
        path = os.path.join(UPLOADS_DIR, os.path.basename(self.filename))
        if not os.path.exists(path):
            return self.blank_white()
        return Image.open(path).convert("RGB")
