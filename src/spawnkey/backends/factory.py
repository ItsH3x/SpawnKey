"""Factory that selects the active WindowBackend and ShortcutBackend for the session."""

from __future__ import annotations

from spawnkey.backends.base import ShortcutBackend, WindowBackend
from spawnkey.backends.detector import BackendKind, EnvironmentInfo, detect_environment
from spawnkey.core.storage import AppPaths


class UnsupportedBackendError(RuntimeError):
    """Raised when the detected desktop backend is not implemented yet."""


def get_backends(
    paths: AppPaths | None = None,
    *,
    env_info: EnvironmentInfo | None = None,
    prefer_portal_shortcuts: bool = False,
) -> tuple[WindowBackend, ShortcutBackend]:
    """Return (WindowBackend, ShortcutBackend) matching the current Linux session."""
    info = env_info or detect_environment()
    resolved_paths = paths or AppPaths.from_env()

    if info.backend is BackendKind.KDE_WAYLAND:
        from spawnkey.backends.kde_wayland import (
            KDEWaylandShortcutBackend,
            KDEWaylandWindowBackend,
        )

        kde_sc = KDEWaylandShortcutBackend(resolved_paths)
        if prefer_portal_shortcuts:
            from spawnkey.backends.portal_shortcuts import XDGPortalShortcutBackend

            sc: ShortcutBackend = XDGPortalShortcutBackend(resolved_paths, delegate=kde_sc)
        else:
            sc = kde_sc
        return (KDEWaylandWindowBackend(resolved_paths), sc)

    if info.backend is BackendKind.GNOME_WAYLAND:
        from spawnkey.backends.gnome_wayland import (
            GnomeWaylandShortcutBackend,
            GnomeWaylandWindowBackend,
        )

        gnome_sc = GnomeWaylandShortcutBackend(resolved_paths)
        if prefer_portal_shortcuts:
            from spawnkey.backends.portal_shortcuts import XDGPortalShortcutBackend

            sc = XDGPortalShortcutBackend(resolved_paths, delegate=gnome_sc)
        else:
            sc = gnome_sc
        return (GnomeWaylandWindowBackend(resolved_paths), sc)

    raise UnsupportedBackendError(
        f"El backend '{info.backend.value}' (sesión={info.session_type}, escritorio={info.desktop}) "
        "aún no está implementado en esta fase. Actualmente activos: kde-wayland, gnome-wayland."
    )
