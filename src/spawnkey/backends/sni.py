"""Shared StatusNotifierItem (SNI) tray activation helpers over D-Bus."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spawnkey.backends.base import token_matches


_GENERIC_SHELL_BINS = frozenset({"python", "python3", "sh", "bash", "env"})


def sni_token_matches(candidate: str, hint: str) -> bool:
    """Generic token matcher aligned with compositor/QML tokenMatches."""
    return token_matches(candidate, hint)


def extract_proc_candidates(pid: int, proc_root: Path = Path("/proc")) -> list[str]:
    """Extract process comm and executable basenames from /proc/<pid>."""
    if pid <= 0:
        return []
    candidates: list[str] = []
    proc_dir = proc_root / str(pid)
    try:
        comm = (proc_dir / "comm").read_text(encoding="utf-8", errors="ignore").strip().lower()
        if comm:
            candidates.append(comm)
    except OSError:
        pass
    try:
        raw_cmd = (proc_dir / "cmdline").read_bytes()
        for arg in raw_cmd.split(b"\x00"):
            if not arg:
                continue
            tok = arg.decode("utf-8", errors="ignore").strip()
            if not tok or tok.startswith("-"):
                continue
            base = Path(tok).name.lower()
            if base and base not in _GENERIC_SHELL_BINS:
                candidates.append(base)
    except OSError:
        pass
    return candidates


def sni_try_activate_tray_app(
    candidate_tokens: list[str],
    *,
    proc_root: Path = Path("/proc"),
    bus: Any = None,
) -> int | None:
    """Wake a resident StatusNotifierItem tray app via D-Bus Activate(0, 0).

    Returns the tray process PID (>= 0) if matched and activated, or None if not found.
    """
    hints = [t.strip().lower() for t in candidate_tokens if t and t.strip()]
    if not hints:
        return None

    if bus is None:
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
        except Exception:
            return None

    items = None
    try:
        items = bus.call_blocking(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            ("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems"),
            timeout=0.8,
        )
    except Exception:
        try:
            items = bus.call_blocking(
                "org.freedesktop.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.freedesktop.DBus.Properties",
                "Get",
                "ss",
                ("org.freedesktop.StatusNotifierWatcher", "RegisteredStatusNotifierItems"),
                timeout=0.8,
            )
        except Exception:
            return None

    if not items:
        return None

    for raw_item in items:
        s_item = str(raw_item)
        if "/" in s_item:
            service, rel_path = s_item.split("/", 1)
            path = "/" + rel_path
        else:
            service, path = s_item, "/StatusNotifierItem"

        pid = 0
        try:
            pid = int(
                bus.call_blocking(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "GetConnectionUnixProcessID",
                    "s",
                    (service,),
                    timeout=0.5,
                )
            )
        except Exception:
            pid = 0

        proc_cands = extract_proc_candidates(pid, proc_root=proc_root)
        matched = any(
            sni_token_matches(cand, hint)
            for cand in proc_cands
            for hint in hints
        )

        all_props: dict[str, Any] = {}
        active_iface = "org.kde.StatusNotifierItem"
        if not matched:
            for iface in ("org.kde.StatusNotifierItem", "org.freedesktop.StatusNotifierItem"):
                try:
                    props = bus.call_blocking(
                        service,
                        path,
                        "org.freedesktop.DBus.Properties",
                        "GetAll",
                        "s",
                        (iface,),
                        timeout=0.5,
                    )
                    if isinstance(props, dict):
                        all_props = {str(k): v for k, v in props.items()}
                        active_iface = iface
                        break
                except Exception:
                    continue
            sni_cands = [
                str(all_props.get("Id", "")).strip().lower(),
                str(all_props.get("Title", "")).strip().lower(),
                str(all_props.get("IconName", "")).strip().lower(),
            ]
            matched = any(
                sni_token_matches(cand, hint)
                for cand in sni_cands
                if cand
                for hint in hints
            )

        if not matched:
            continue

        # 1. Standard StatusNotifierItem.Activate(x, y)
        for iface in (active_iface, "org.kde.StatusNotifierItem", "org.freedesktop.StatusNotifierItem"):
            try:
                bus.call_blocking(
                    service,
                    path,
                    iface,
                    "Activate",
                    "ii",
                    (0, 0),
                    timeout=0.8,
                )
                return pid
            except Exception:
                continue

        # 2. Fallback for legacy libappindicator items exposing only com.canonical.dbusmenu
        menu_path = str(all_props.get("Menu", ""))
        if not menu_path:
            try:
                menu_path = str(
                    bus.call_blocking(
                        service,
                        path,
                        "org.freedesktop.DBus.Properties",
                        "Get",
                        "ss",
                        (active_iface, "Menu"),
                        timeout=0.5,
                    )
                )
            except Exception:
                menu_path = ""
        if menu_path and menu_path != "/":
            try:
                import dbus  # type: ignore[import-not-found]

                layout = bus.call_blocking(
                    service,
                    menu_path,
                    "com.canonical.dbusmenu",
                    "GetLayout",
                    "iias",
                    (0, 1, ["label", "enabled", "visible", "type"]),
                    timeout=0.8,
                )
                children = layout[1][2] if layout and len(layout) > 1 and len(layout[1]) > 2 else []
                for child in children:
                    item_id = int(child[0])
                    c_props = child[1] if len(child) > 1 else {}
                    if str(c_props.get("type", "")) == "separator":
                        continue
                    if not bool(c_props.get("enabled", True)) or not bool(c_props.get("visible", True)):
                        continue
                    bus.call_blocking(
                        service,
                        menu_path,
                        "com.canonical.dbusmenu",
                        "Event",
                        "isvu",
                        (item_id, "clicked", dbus.String("", variant_level=1), 0),
                        timeout=0.8,
                    )
                    return pid
            except Exception:
                pass

    return None
