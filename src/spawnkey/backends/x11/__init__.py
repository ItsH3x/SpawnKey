"""Universal X11 window and shortcut backends for SpawnKey."""

from __future__ import annotations

from spawnkey.backends.x11.shortcuts import X11ShortcutBackend, shortcut_to_x11_accel
from spawnkey.backends.x11.window_backend import X11WindowBackend, parse_xrandr_outputs

__all__ = [
    "X11ShortcutBackend",
    "X11WindowBackend",
    "parse_xrandr_outputs",
    "shortcut_to_x11_accel",
]
