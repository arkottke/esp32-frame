from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from plugins.base import Plugin

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


@dataclass
class _DirState:
    seen: set[str] = field(default_factory=set)
    queue: list[str] = field(default_factory=list)
    queued: set[str] = field(default_factory=set)


_STATE_FILE = Path(__file__).parent.parent / "gallery_state.json"
_state: dict[str, _DirState] = {}


def _load_state() -> None:
    if not _STATE_FILE.exists():
        return
    try:
        data = json.loads(_STATE_FILE.read_text())
    except Exception:
        return
    for dir_path, d in data.items():
        s = _DirState()
        s.seen   = set(d.get("seen", []))
        s.queue  = list(d.get("queue", []))
        s.queued = set(s.queue)
        _state[dir_path] = s


def _save_state() -> None:
    try:
        data = {
            path: {"seen": list(s.seen), "queue": s.queue}
            for path, s in _state.items()
        }
        _STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


_load_state()


class GalleryPlugin(Plugin):
    plugin_type = "gallery"

    def __init__(self, config):
        super().__init__(config)
        dirs = config.get("directories", [])
        if isinstance(dirs, str):
            dirs = [d.strip() for d in dirs.splitlines() if d.strip()]
        self._directories = [str(Path(d).resolve()) for d in dirs if d]
        self._zoom = config.get("zoom", "fill")
        self._rotations: dict[str, int] = config.get("rotations", {})

    def _scan_dir(self, dir_path: str) -> list[str]:
        p = Path(dir_path)
        if not p.is_dir():
            return []
        files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in _IMAGE_EXTS]
        return sorted((str(f) for f in files), key=os.path.getmtime)

    def render(self) -> Image.Image:
        try:
            return self._render()
        except Exception:
            return Plugin.blank_white()

    def _render(self) -> Image.Image:
        # Enqueue newly discovered files in all directories
        for dir_path in self._directories:
            if dir_path not in _state:
                _state[dir_path] = _DirState()
            state = _state[dir_path]
            for f in self._scan_dir(dir_path):
                if f not in state.seen and f not in state.queued:
                    state.queue.append(f)
                    state.queued.add(f)

        # Pick next unseen image (first directory wins)
        chosen = None
        for dir_path in self._directories:
            state = _state[dir_path]
            if state.queue:
                chosen = state.queue.pop(0)
                state.queued.discard(chosen)
                state.seen.add(chosen)
                break

        # Fall back to random selection across all dirs
        if chosen is None:
            all_files: list[str] = []
            for dir_path in self._directories:
                all_files.extend(self._scan_dir(dir_path))
            if not all_files:
                return Plugin.blank_white()
            chosen = random.choice(all_files)

        _save_state()
        img = Image.open(chosen).convert("RGB")

        rotation = self._rotations.get(Path(chosen).name, 0)
        if rotation:
            img = img.rotate(-rotation, expand=True)

        target = (1200, 1600)
        if self._zoom == "fit":
            img.thumbnail(target, Image.LANCZOS)
            bg = Image.new("RGB", target, (0, 0, 0))
            bg.paste(img, ((target[0] - img.width) // 2, (target[1] - img.height) // 2))
            img = bg
        elif self._zoom == "stretch":
            img = img.resize(target, Image.LANCZOS)
        elif self._zoom == "center":
            bg = Image.new("RGB", target, (0, 0, 0))
            if img.width > target[0] or img.height > target[1]:
                left = max(0, (img.width - target[0]) // 2)
                top = max(0, (img.height - target[1]) // 2)
                img = img.crop((left, top, left + min(img.width, target[0]), top + min(img.height, target[1])))
            bg.paste(img, ((target[0] - img.width) // 2, (target[1] - img.height) // 2))
            img = bg
        else:
            img = ImageOps.fit(img, target, Image.LANCZOS)

        return img.convert("RGB")
