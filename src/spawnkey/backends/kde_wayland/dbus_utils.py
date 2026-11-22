"""D-Bus helpers for KDE Plasma 6 with fast in-process SessionBus and qdbus6 fallback."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Any

from spawnkey.backends.base import BackendError


class DBusError(BackendError):
    """Raised when a KDE D-Bus call fails."""


class _InProcessDBusClient:
    """Thin wrapper around a single dbus.SessionBus() connection for zero-subprocess calls."""

    def __init__(self) -> None:
        import dbus  # type: ignore[import-not-found]

        self._bus = dbus.SessionBus()

    def call(self, service: str, path: str, method: str, *args: Any) -> Any:
        if "." in method:
            iface_name, member = method.rsplit(".", 1)
        elif service == "org.kde.KWin" and path.startswith("/Scripting/Script"):
            iface_name, member = "org.kde.kwin.Script", method
        else:
            iface_name, member = None, method

        # Pass explicit string signature to support overloaded KWin methods
        # (e.g. loadScript(s) vs loadScript(ss)) and skip XML introspection.
        signature = "s" * len(args)
        return self._bus.call_blocking(
            service,
            path,
            iface_name,
            member,
            signature,
            args,
            timeout=5.0,
        )


_DBUS_CLIENT: _InProcessDBusClient | None = None
_DBUS_UNAVAILABLE = False


def reset_dbus_client() -> None:
    """Reset cached in-process D-Bus client (used in tests or after bus reconnect)."""
    global _DBUS_CLIENT, _DBUS_UNAVAILABLE
    _DBUS_CLIENT = None
    _DBUS_UNAVAILABLE = False


def _get_dbus_client() -> _InProcessDBusClient | None:
    global _DBUS_CLIENT, _DBUS_UNAVAILABLE
    if _DBUS_UNAVAILABLE:
        return None
    if _DBUS_CLIENT is None:
        try:
            _DBUS_CLIENT = _InProcessDBusClient()
        except Exception:
            _DBUS_UNAVAILABLE = True
            return None
    return _DBUS_CLIENT


def find_qdbus() -> str:
    for candidate in ("qdbus6", "qdbus-qt6", "qdbus"):
        found = shutil.which(candidate)
        if found:
            return found
    raise DBusError("No se encontró 'qdbus6' ni 'qdbus' en PATH.")


def find_busctl() -> str:
    found = shutil.which("busctl")
    if not found:
        raise DBusError("No se encontró 'busctl' en PATH.")
    return found


def run_cmd(cmd: list[str], *, check: bool = True, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise DBusError(f"Comando fallido ({' '.join(cmd)}): {err}")
    return proc


def qdbus_call(service: str, path: str, method: str, *args: str, check: bool = True) -> str:
    """Invoke a D-Bus method using the in-process SessionBus when available, else qdbus6."""
    client = _get_dbus_client()
    if client is not None:
        try:
            res = client.call(service, path, method, *args)
            if res is None:
                return ""
            # dbus-python returns dbus.Boolean (a subclass of int, not bool),
            # whose str() is "1"/"0" instead of "true"/"false".
            if isinstance(res, bool) or type(res).__name__ == "Boolean":
                return "true" if bool(res) else "false"
            return str(res).strip()
        except Exception as exc:
            if check:
                raise DBusError(f"Llamada D-Bus fallida ({service} {path} {method}): {exc}") from exc
            return ""

    qdbus = find_qdbus()
    proc = run_cmd([qdbus, service, path, method, *args], check=check)
    return proc.stdout.strip()


def kwin_is_script_loaded(plugin_name: str) -> bool:
    out = qdbus_call(
        "org.kde.KWin",
        "/Scripting",
        "org.kde.kwin.Scripting.isScriptLoaded",
        plugin_name,
        check=False,
    )
    return out.strip().lower() == "true"


def kwin_unload_script(plugin_name: str) -> bool:
    out = qdbus_call(
        "org.kde.KWin",
        "/Scripting",
        "org.kde.kwin.Scripting.unloadScript",
        plugin_name,
        check=False,
    )
    return out.strip().lower() == "true"


def kwin_stop_script(script_id: int) -> None:
    """Explicitly stop a KWin script object so its /Scripting/Script{id} D-Bus path is released."""
    if script_id < 0:
        return
    qdbus_call("org.kde.KWin", f"/Scripting/Script{script_id}", "stop", check=False)


def kwin_stop_and_unload(plugin_name: str, script_id: int | None = None) -> bool:
    """Stop a KWin script by ID (if still loaded) and unload it by plugin name."""
    if script_id is not None and script_id >= 0 and kwin_is_script_loaded(plugin_name):
        kwin_stop_script(script_id)
    return kwin_unload_script(plugin_name)


def kwin_load_js(
    js_path: Path,
    plugin_name: str,
    *,
    run: bool = True,
    unload_first: bool = True,
) -> int:
    """Load js_path under plugin_name and optionally run it."""
    if unload_first:
        kwin_unload_script(plugin_name)
    out = qdbus_call(
        "org.kde.KWin",
        "/Scripting",
        "org.kde.kwin.Scripting.loadScript",
        str(js_path),
        plugin_name,
    )
    try:
        script_id = int(out.strip())
    except ValueError as exc:
        raise DBusError(f"Respuesta inesperada de KWin loadScript: {out!r}") from exc

    if script_id < 0:
        raise DBusError(f"KWin rechazó el script JS '{js_path}' (id={script_id}).")

    if run:
        qdbus_call("org.kde.KWin", f"/Scripting/Script{script_id}", "run")
    return script_id


def kwin_load_and_run_declarative(
    qml_path: Path,
    plugin_name: str,
    *,
    unload_first: bool = True,
) -> int:
    """Load declarative QML script under plugin_name and run it."""
    if unload_first:
        kwin_unload_script(plugin_name)

    out = qdbus_call(
        "org.kde.KWin",
        "/Scripting",
        "org.kde.kwin.Scripting.loadDeclarativeScript",
        str(qml_path),
        plugin_name,
    )
    try:
        script_id = int(out.strip())
    except ValueError as exc:
        raise DBusError(f"Respuesta inesperada de KWin loadDeclarativeScript: {out!r}") from exc

    if script_id < 0:
        raise DBusError(f"KWin rechazó el script declarativo '{qml_path}' (id={script_id}).")

    qdbus_call("org.kde.KWin", f"/Scripting/Script{script_id}", "run")
    return script_id


def kwin_is_effect_loaded(effect_name: str) -> bool:
    """Return True if the named KWin effect is currently loaded in org.kde.KWin /Effects."""
    out = qdbus_call(
        "org.kde.KWin",
        "/Effects",
        "org.kde.kwin.Effects.isEffectLoaded",
        effect_name,
        check=False,
    )
    return out.strip().lower() == "true"


def kwin_load_effect(effect_name: str) -> bool:
    """Ask KWin to load the named effect plugin via org.kde.KWin /Effects."""
    out = qdbus_call(
        "org.kde.KWin",
        "/Effects",
        "org.kde.kwin.Effects.loadEffect",
        effect_name,
        check=False,
    )
    return out.strip().lower() == "true"


def kwin_reconfigure_effect(effect_name: str) -> None:
    """Trigger Effect::reconfigure on a loaded KWin effect plugin."""
    qdbus_call(
        "org.kde.KWin",
        "/Effects",
        "org.kde.kwin.Effects.reconfigureEffect",
        effect_name,
        check=False,
    )


def _sni_token_matches(candidate: str, hint: str) -> bool:
    """Generic token matcher aligned with KWin QML tokenMatches (no app-specific rules)."""
    if not candidate or not hint:
        return False
    norm_cand = candidate.strip().lower().replace("_", "-")
    norm_hint = hint.strip().lower().replace("_", "-")
    if not norm_cand or not norm_hint:
        return False
    if norm_cand == norm_hint or norm_hint in norm_cand:
        return True
    if len(norm_cand) >= 3 and norm_cand in norm_hint:
        return True
    dash_idx = norm_hint.find("-")
    if dash_idx >= 4:
        stem = norm_hint[:dash_idx]
        if norm_cand == stem or stem in norm_cand:
            return True
    if "." in norm_hint:
        ignored = {"desktop", "client", "linux", "github", "com", "org", "net", "io"}
        for seg in reversed(norm_hint.split(".")):
            if len(seg) < 4 or seg in ignored:
                continue
            if norm_cand == seg or seg in norm_cand:
                return True
            seg_dash = seg.find("-")
            if seg_dash >= 4:
                seg_stem = seg[:seg_dash]
                if norm_cand == seg_stem or seg_stem in norm_cand:
                    return True
    return False


def _extract_proc_candidates(pid: int, proc_root: Path = Path("/proc")) -> list[str]:
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
            if base and base not in ("python", "python3", "sh", "bash", "env"):
                candidates.append(base)
    except OSError:
        pass
    return candidates


def sni_try_activate_tray_app(
    candidate_tokens: list[str],
    *,
    proc_root: Path = Path("/proc"),
) -> int | None:
    """Wake a resident StatusNotifierItem tray app via D-Bus Activate(0, 0).

    Returns the tray process PID (>= 0) if matched and activated, or None if not found.
    """
    hints = [t.strip().lower() for t in candidate_tokens if t and t.strip()]
    if not hints:
        return None
    client = _get_dbus_client()
    if client is None:
        return None
    bus = client._bus
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

        proc_cands = _extract_proc_candidates(pid, proc_root=proc_root)
        matched = any(
            _sni_token_matches(cand, hint)
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
                _sni_token_matches(cand, hint)
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

