"""Compositor/display-server backends and environment detection."""

from spawnkey.backends.base import (
    ShortcutBackend,
    ShortcutRegistrationStatus,
    WindowBackend,
)
from spawnkey.backends.detector import BackendKind, EnvironmentInfo, detect_environment

__all__ = [
    "BackendKind",
    "EnvironmentInfo",
    "ShortcutBackend",
    "ShortcutRegistrationStatus",
    "WindowBackend",
    "detect_environment",
]
