"""KDE Plasma 6 Wayland backend (KWin 6 scripting + kglobalaccel shortcuts)."""

from spawnkey.backends.kde_wayland.shortcuts import KDEWaylandShortcutBackend
from spawnkey.backends.kde_wayland.window_backend import KDEWaylandWindowBackend

__all__ = [
    "KDEWaylandShortcutBackend",
    "KDEWaylandWindowBackend",
]
