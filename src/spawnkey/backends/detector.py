"""Automatic detection of the active Linux session and desktop environment."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import shutil
from typing import Mapping


class BackendKind(str, Enum):
    KDE_WAYLAND = "kde-wayland"
    GNOME_WAYLAND = "gnome-wayland"
    HYPRLAND = "hyprland"
    SWAY = "sway"
    X11 = "x11"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EnvironmentInfo:
    session_type: str
    desktop: str
    backend: BackendKind
    tools: dict[str, bool]


def _probe_tools() -> dict[str, bool]:
    candidates = [
        "kwriteconfig6",
        "kwriteconfig5",
        "qdbus6",
        "qdbus",
        "busctl",
        "gsettings",
        "hyprctl",
        "swaymsg",
        "xdotool",
        "xrandr",
    ]
    return {tool: shutil.which(tool) is not None for tool in candidates}


def detect_environment(
    env: Mapping[str, str] | None = None,
    *,
    tools: dict[str, bool] | None = None,
) -> EnvironmentInfo:
    """Detect the active display server and recommended SpawnKey backend."""
    source = env if env is not None else os.environ
    session_type = source.get("XDG_SESSION_TYPE", "").strip().lower()
    desktop_raw = (
        source.get("XDG_CURRENT_DESKTOP")
        or source.get("DESKTOP_SESSION")
        or ""
    ).strip()
    desktop_tokens = {token.strip().lower() for token in desktop_raw.split(":") if token.strip()}

    detected_tools = tools if tools is not None else _probe_tools()

    if not session_type:
        if source.get("WAYLAND_DISPLAY"):
            session_type = "wayland"
        elif source.get("DISPLAY"):
            session_type = "x11"

    backend = BackendKind.UNKNOWN
    if session_type == "x11":
        backend = BackendKind.X11
    elif session_type == "wayland":
        if source.get("HYPRLAND_INSTANCE_SIGNATURE") or "hyprland" in desktop_tokens:
            backend = BackendKind.HYPRLAND
        elif source.get("SWAYSOCK") or "sway" in desktop_tokens:
            backend = BackendKind.SWAY
        elif "kde" in desktop_tokens or "plasma" in desktop_tokens:
            backend = BackendKind.KDE_WAYLAND
        elif "gnome" in desktop_tokens or "ubuntu" in desktop_tokens:
            backend = BackendKind.GNOME_WAYLAND

    return EnvironmentInfo(
        session_type=session_type or "unknown",
        desktop=desktop_raw or "unknown",
        backend=backend,
        tools=detected_tools,
    )
