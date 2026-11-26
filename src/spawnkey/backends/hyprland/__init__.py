"""Hyprland Wayland window and shortcut backends."""

from spawnkey.backends.hyprland.shortcuts import (
    HyprlandShortcutBackend,
    shortcut_to_hyprland_bind,
)
from spawnkey.backends.hyprland.window_backend import (
    HyprlandWindowBackend,
    parse_hyprland_monitors,
)

__all__ = [
    "HyprlandShortcutBackend",
    "HyprlandWindowBackend",
    "parse_hyprland_monitors",
    "shortcut_to_hyprland_bind",
]
