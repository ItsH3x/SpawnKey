"""GNOME Wayland window backend (Mutter DisplayConfig + opt-in Shell bridge / process fallback).

Technical note on GNOME Shell 45+ (Mutter Wayland):
- Standard Wayland `xdg-shell` does not permit clients to position top-level windows (`x, y`),
  strip server decorations of arbitrary processes, or force `keep_above` from outside the compositor.
- GNOME Shell 41+ disables unprivileged `org.gnome.Shell.Eval` by default unless an opt-in GNOME
  Shell extension (or development `unsafe_mode`) exposes a window control D-Bus interface.
- This backend queries real monitor geometries via `org.gnome.Mutter.DisplayConfig.GetCurrentState`,
  respects `profile.monitor_policy`, drives full dropdown geometry/opacity/flags when the opt-in
  SpawnKey GNOME Shell extension (`io.github.spawnkey.GnomeShell`) or `org.gnome.Shell.Eval` is
  available, and falls back cleanly to zero-daemon process lifecycle management (`persistent`,
  `ephemeral`, `tray`) when running on stock locked-down GNOME Wayland.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Any

from spawnkey.backends.base import (
    ENV_MARKER,
    BackendCapabilities,
    WindowBackend,
    build_launch_argv,
    find_profile_pids,
    is_pid_alive_for_profile,
    prepare_launch_argv_and_env,
    profile_match_tokens,
)
from spawnkey.backends.sni import sni_try_activate_tray_app
from spawnkey.core.desktop_apps import default_application_dirs
from spawnkey.core.geometry import (
    compute_animation_duration_ms,
    compute_geometry,
    select_monitor_by_policy,
)
from spawnkey.core.models import (
    DropdownProfile,
    LifecycleMode,
    MonitorRect,
    RuntimeState,
    WindowGeometry,
)
from spawnkey.core.storage import AppPaths, profile_run_guard

EXTENSION_UUID = "spawnkey@spawnkey.github.io"
EXTENSION_BUS_NAME = "io.github.spawnkey.GnomeShell"
EXTENSION_OBJ_PATH = "/io/github/spawnkey/GnomeShell"
EXTENSION_IFACE = "io.github.spawnkey.GnomeShell"

_CORNER_RADIUS_HINT = "Mutter usa esquinas nativas del cliente en Wayland"
_HINT_EXT_INSTALLED_INACTIVE = (
    "Extensión GNOME instalada pero inactiva; actívala con "
    "'gnome-extensions enable spawnkey@spawnkey.github.io' y reinicia sesión"
)
_HINT_EXT_NOT_INSTALLED = (
    "Instala la extensión ligera con './INSTALL.sh --gnome-extension' para "
    "posicionamiento, opacidad y animaciones en GNOME Wayland"
)

_ROTATED_TRANSFORMS = frozenset({1, 3, 5, 7})


def _extract_physical_monitor_map(
    physical_monitors: Any,
) -> tuple[dict[tuple[str, str, str, str], tuple[int, int]], dict[str, tuple[int, int]]]:
    by_tuple: dict[tuple[str, str, str, str], tuple[int, int]] = {}
    by_connector: dict[str, tuple[int, int]] = {}
    if not isinstance(physical_monitors, (list, tuple)):
        return by_tuple, by_connector

    for entry in physical_monitors:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        ident = entry[0]
        modes = entry[1]
        if not isinstance(ident, (list, tuple)) or not ident or not isinstance(modes, (list, tuple)):
            continue

        current_mode: Any = None
        preferred_mode: Any = None
        first_mode: Any = None
        for mode in modes:
            if not isinstance(mode, (list, tuple)) or len(mode) < 3:
                continue
            if first_mode is None:
                first_mode = mode
            mode_props = mode[-1] if isinstance(mode[-1], dict) else {}
            if mode_props.get("is-current"):
                current_mode = mode
                break
            if preferred_mode is None and mode_props.get("is-preferred"):
                preferred_mode = mode

        chosen = current_mode or preferred_mode or first_mode
        if chosen is None:
            continue
        try:
            w_px = int(chosen[1])
            h_px = int(chosen[2])
        except (TypeError, ValueError):
            continue
        if w_px <= 1 or h_px <= 1:
            continue

        connector = str(ident[0])
        by_connector[connector] = (w_px, h_px)
        if len(ident) >= 4:
            key = (str(ident[0]), str(ident[1]), str(ident[2]), str(ident[3]))
            by_tuple[key] = (w_px, h_px)

    return by_tuple, by_connector


def parse_mutter_logical_monitors(
    logical_monitors: Any,
    physical_monitors: Any = None,
) -> tuple[list[MonitorRect], int]:
    """Parse `org.gnome.Mutter.DisplayConfig.GetCurrentState` logical and physical monitors."""
    monitors: list[MonitorRect] = []
    primary_idx = 0
    if not isinstance(logical_monitors, (list, tuple)):
        return monitors, primary_idx

    by_tuple, by_connector = _extract_physical_monitor_map(physical_monitors)

    for idx, item in enumerate(logical_monitors):
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        x = int(item[0])
        y = int(item[1])
        raw_scale = float(item[2]) if item[2] else 1.0
        scale = raw_scale if raw_scale > 0 else 1.0
        transform = int(item[3]) if item[3] is not None else 0
        is_primary = bool(item[4])
        connectors = item[5]
        props = item[6] if len(item) > 6 and isinstance(item[6], dict) else {}

        conn_name = f"gnome-output-{idx}"
        conn_name_set = False
        phys_dims: tuple[int, int] | None = None

        if isinstance(connectors, (list, tuple)) and connectors:
            for conn in connectors:
                if isinstance(conn, (list, tuple)) and conn:
                    conn_str = str(conn[0])
                    if not conn_name_set:
                        conn_name = conn_str
                        conn_name_set = True
                    if len(conn) >= 4:
                        tkey = (str(conn[0]), str(conn[1]), str(conn[2]), str(conn[3]))
                        phys_dims = by_tuple.get(tkey)
                    if phys_dims is None:
                        phys_dims = by_connector.get(conn_str)
                    if phys_dims is not None:
                        break
                elif isinstance(conn, str) and conn:
                    if not conn_name_set:
                        conn_name = conn
                        conn_name_set = True
                    phys_dims = by_connector.get(conn)
                    if phys_dims is not None:
                        break

        if phys_dims is not None:
            phys_w, phys_h = phys_dims
            if transform in _ROTATED_TRANSFORMS:
                phys_w, phys_h = phys_h, phys_w
            width = max(2, round(phys_w / scale))
            height = max(2, round(phys_h / scale))
        else:
            width = int(props.get("width", 1920)) if props else 1920
            height = int(props.get("height", 1080)) if props else 1080

        if width <= 1 or height <= 1:
            continue
        if is_primary:
            primary_idx = len(monitors)
        monitors.append(MonitorRect(x=x, y=y, width=width, height=height, name=conn_name))

    return monitors, primary_idx


def render_gnome_shell_eval_js(
    profile: DropdownProfile,
    target: WindowGeometry,
    *,
    action: str,
    candidate_pids: list[int],
) -> str:
    """Render an ephemeral GNOME Shell GJS snippet for window placement and visibility control."""
    payload = {
        "action": action,  # "show" | "hide" | "toggle"
        "mode": profile.mode.value,
        "matchHint": (profile.match_hint or "").strip().lower(),
        "desktopId": (profile.desktop_id or "").strip().lower(),
        "matchTokens": profile_match_tokens(profile),
        "pids": candidate_pids,
        "x": target.x,
        "y": target.y,
        "width": target.width,
        "height": target.height,
        "keepAbove": profile.keep_above,
        "showInTaskbar": profile.show_in_taskbar,
        "opacity": max(26, min(255, round(float(profile.opacity) * 255))),
        "monitorPolicy": profile.monitor_policy.value,
        "relocateOnToggle": bool(getattr(profile, "relocate_on_toggle", True)),
    }
    cfg_json = json.dumps(payload, ensure_ascii=False)
    return f"""(function() {{
    const cfg = {cfg_json};
    const actors = global.get_window_actors ? global.get_window_actors() : [];
    const ignoredSegs = new Set(["org", "com", "net", "io", "app", "desktop", "client", "linux", "github"]);
    function normTok(v) {{
        return String(v || "").trim().toLowerCase().replace(/_/g, "-");
    }}
    function tokenMatches(cand, hint) {{
        const nc = normTok(cand);
        const nh = normTok(hint);
        if (!nc || !nh) return false;
        if (nc === nh || nc.indexOf(nh) !== -1) return true;
        if (nc.length >= 3 && nh.indexOf(nc) !== -1) return true;
        const dash = nh.indexOf("-");
        if (dash >= 4) {{
            const stem = nh.slice(0, dash);
            if (nc === stem || nc.indexOf(stem) !== -1) return true;
        }}
        if (nh.indexOf(".") !== -1) {{
            const parts = nh.split(".");
            for (let p = parts.length - 1; p >= 0; p--) {{
                const seg = parts[p];
                if (seg.length < 4 || ignoredSegs.has(seg)) continue;
                if (nc === seg || nc.indexOf(seg) !== -1) return true;
                const sdash = seg.indexOf("-");
                if (sdash >= 4) {{
                    const sstem = seg.slice(0, sdash);
                    if (nc === sstem || nc.indexOf(sstem) !== -1) return true;
                }}
            }}
        }}
        return false;
    }}
    function collectTokens() {{
        const out = [];
        if (Array.isArray(cfg.matchTokens)) {{
            for (const t of cfg.matchTokens) {{
                const s = String(t || "").trim().toLowerCase();
                if (s && out.indexOf(s) === -1) out.push(s);
            }}
        }}
        if (cfg.matchHint && out.indexOf(cfg.matchHint) === -1) out.push(cfg.matchHint);
        let desk = String(cfg.desktopId || "");
        if (desk.endsWith(".desktop")) desk = desk.slice(0, -8);
        if (desk && out.indexOf(desk) === -1) out.push(desk);
        return out;
    }}
    const tokens = collectTokens();
    function matches(win) {{
        if (!win) return false;
        const pid = win.get_pid ? win.get_pid() : -1;
        if (cfg.pids && cfg.pids.indexOf(pid) !== -1) return true;
        const cands = [
            win.get_wm_class ? (win.get_wm_class() || "") : "",
            win.get_wm_class_instance ? (win.get_wm_class_instance() || "") : "",
            win.get_gtk_application_id ? (win.get_gtk_application_id() || "") : "",
            win.get_sandboxed_app_id ? (win.get_sandboxed_app_id() || "") : ""
        ];
        for (const cand of cands) {{
            if (!cand) continue;
            for (const tok of tokens) {{
                if (tokenMatches(cand, tok)) return true;
            }}
        }}
        return false;
    }}
    function shouldRelocateOrFocus(win) {{
        if (!win || cfg.relocateOnToggle === false) return false;
        const policy = String(cfg.monitorPolicy || "cursor");
        if (policy !== "cursor" && policy !== "focused") return false;
        let targetMon = -1;
        if (policy === "focused") {{
            const focused = global.display.focus_window;
            targetMon = focused ? focused.get_monitor() : global.display.get_current_monitor();
        }} else {{
            targetMon = global.display.get_current_monitor();
        }}
        if (typeof win.get_monitor === "function" && targetMon >= 0 && win.get_monitor() !== targetMon) {{
            return true;
        }}
        const focused = global.display.focus_window;
        if (focused !== win && focused?.get_transient_for?.() !== win) {{
            return true;
        }}
        return false;
    }}
    for (let i = actors.length - 1; i >= 0; i--) {{
        const actor = actors[i];
        const win = actor.get_meta_window ? actor.get_meta_window() : null;
        if (!matches(win)) continue;
        if (cfg.action === "hide" || (cfg.action === "toggle" && !win.minimized && !shouldRelocateOrFocus(win))) {{
            if (cfg.mode === "ephemeral" || cfg.mode === "tray") {{
                win.delete(global.get_current_time());
            }} else {{
                win.minimize();
            }}
            return "hidden";
        }}
        if (win.minimized) win.unminimize();
        const isMax = (typeof win.is_maximized === "function")
            ? win.is_maximized()
            : (typeof win.get_maximized === "function" ? win.get_maximized() !== 0 : false);
        if (isMax && typeof win.unmaximize === "function") {{
            try {{
                if (win.unmaximize.length === 0) win.unmaximize();
                else win.unmaximize(3);
            }} catch (_e) {{
                try {{ win.unmaximize(); }} catch (_e2) {{}}
            }}
        }}
        if (win.fullscreen && typeof win.unmake_fullscreen === "function") win.unmake_fullscreen();
        win.move_resize_frame(true, cfg.x, cfg.y, cfg.width, cfg.height);
        if (cfg.keepAbove && win.make_above) win.make_above();
        else if (!cfg.keepAbove && win.unmake_above) win.unmake_above();
        if (win.stick) win.stick();
        if (actor.set_opacity) actor.set_opacity(cfg.opacity);
        win.activate(global.get_current_time());
        return "shown";
    }}
    return "not_found";
}})();"""


class _CapabilitiesDescriptor:
    """Returns default capabilities on class access and live-probed capabilities on instances."""

    def __init__(self, default: BackendCapabilities) -> None:
        self.default = default

    def __get__(
        self,
        instance: "GnomeWaylandWindowBackend | None",
        owner: type | None = None,
    ) -> BackendCapabilities:
        if instance is None:
            return self.default
        return instance._get_capabilities()


class GnomeWaylandWindowBackend(WindowBackend):
    """WindowBackend for GNOME on Wayland respecting zero resident daemon."""

    name = "gnome-wayland"
    capabilities = _CapabilitiesDescriptor(  # type: ignore[assignment]
        BackendCapabilities(
            supports_opacity=False,
            supports_corner_radius=False,
            supports_click_outside=False,
            supports_animations=False,
        )
    )

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.from_env()
        self._caps_cache: BackendCapabilities | None = None

    @staticmethod
    def _candidate_extension_dirs() -> list[Path]:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        candidates = [
            data_home / "gnome-shell" / "extensions" / EXTENSION_UUID,
            Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / EXTENSION_UUID,
            Path("/usr/local/share/gnome-shell/extensions") / EXTENSION_UUID,
            Path("/usr/share/gnome-shell/extensions") / EXTENSION_UUID,
        ]
        unique: list[Path] = []
        for path in candidates:
            if path not in unique:
                unique.append(path)
        return unique

    def _is_extension_installed(self) -> bool:
        return any(path.is_dir() for path in self._candidate_extension_dirs())

    def _probe_extension_ping(self) -> str | None:
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            ext_obj = bus.get_object(EXTENSION_BUS_NAME, EXTENSION_OBJ_PATH)
            ext_iface = dbus.Interface(ext_obj, EXTENSION_IFACE)
            version = str(ext_iface.Ping()).strip()
            return version or None
        except Exception:
            return None

    def _probe_shell_eval_available(self) -> bool:
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            shell_obj = bus.get_object("org.gnome.Shell", "/org/gnome/Shell")
            shell_iface = dbus.Interface(shell_obj, "org.gnome.Shell")
            ok, _ = shell_iface.Eval("1")
            return bool(ok)
        except Exception:
            return False

    def _get_capabilities(self) -> BackendCapabilities:
        if self._caps_cache is not None:
            return self._caps_cache

        if self._probe_extension_ping() is not None or self._probe_shell_eval_available():
            self._caps_cache = BackendCapabilities(
                supports_opacity=True,
                supports_corner_radius=False,
                supports_click_outside=True,
                supports_animations=True,
                corner_radius_hint=_CORNER_RADIUS_HINT,
                backend_hint="",
            )
            return self._caps_cache

        hint = (
            _HINT_EXT_INSTALLED_INACTIVE
            if self._is_extension_installed()
            else _HINT_EXT_NOT_INSTALLED
        )
        self._caps_cache = BackendCapabilities(
            supports_opacity=False,
            supports_corner_radius=False,
            supports_click_outside=False,
            supports_animations=False,
            corner_radius_hint="",
            backend_hint=hint,
        )
        return self._caps_cache

    def refresh_capabilities(self) -> BackendCapabilities:
        """Re-probe GNOME Shell extension D-Bus and Shell.Eval availability."""
        self._caps_cache = None
        return self._get_capabilities()

    def _query_mutter_monitors(self) -> tuple[list[MonitorRect], int]:
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            obj = bus.get_object("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig")
            iface = dbus.Interface(obj, "org.gnome.Mutter.DisplayConfig")
            state = iface.GetCurrentState()
            if isinstance(state, (list, tuple)) and len(state) >= 3:
                return parse_mutter_logical_monitors(state[2], state[1])
        except Exception:
            pass
        return [], 0

    def get_active_monitor(self, profile: DropdownProfile) -> MonitorRect:
        """Return active monitor rectangle on GNOME Wayland honoring profile.monitor_policy."""
        monitors, primary_idx = self._query_mutter_monitors()
        if monitors:
            return select_monitor_by_policy(
                monitors,
                profile.monitor_policy,
                primary_index=primary_idx,
            )
        return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    def orig_state_path(self, profile_id: str) -> Path:
        """Return volatile path $XDG_RUNTIME_DIR/spawnkey/<id>.orig.json for tray original state."""
        return self.paths.state_dir / f"{profile_id}.orig.json"

    def load_orig_state(self, profile_id: str) -> dict[str, Any] | None:
        """Load volatile original window state for a tray profile if present."""
        path = self.orig_state_path(profile_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return None

    def save_orig_state(self, profile_id: str, payload: dict[str, Any]) -> Path:
        """Atomically persist original window state to $XDG_RUNTIME_DIR/spawnkey/<id>.orig.json."""
        path = self.orig_state_path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        clean_payload = dict(payload)
        clean_payload["profile_id"] = profile_id
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(clean_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    def clear_orig_state(self, profile_id: str) -> bool:
        """Remove volatile $XDG_RUNTIME_DIR/spawnkey/<id>.orig.json after clean restore."""
        path = self.orig_state_path(profile_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def _build_extension_payload(
        self,
        profile: DropdownProfile,
        state: RuntimeState,
        *,
        action: str,
        candidate_pids: list[int],
    ) -> dict[str, Any]:
        return {
            "action": action,
            "profileId": profile.id,
            "mode": profile.mode.value,
            "startupTimeoutMs": max(1000, int(profile.startup_timeout_ms)),
            "sizeUnit": profile.size_unit.value,
            "width": profile.width,
            "height": profile.height,
            "positionMode": profile.position_mode.value,
            "anchorH": profile.anchor_h.value,
            "anchorV": profile.anchor_v.value,
            "edgeMargin": max(0, int(profile.edge_margin)),
            "customPositionUnit": profile.custom_position_unit.value,
            "customX": profile.custom_x,
            "customY": profile.custom_y,
            "monitorPolicy": profile.monitor_policy.value,
            "relocateOnToggle": bool(getattr(profile, "relocate_on_toggle", True)),
            "hideOnClickOutside": profile.hide_on_click_outside,
            "showInTaskbar": profile.show_in_taskbar,
            "keepAbove": profile.keep_above,
            "opacity": round(float(profile.opacity), 3),
            "direction": profile.direction.value,
            "animationType": profile.animation_type.value,
            "durationMs": compute_animation_duration_ms(profile),
            "matchHint": (profile.match_hint or "").strip().lower(),
            "desktopId": (profile.desktop_id or "").strip().lower(),
            "matchTokens": profile_match_tokens(profile),
            "pids": candidate_pids,
            "windowId": state.window_id or "",
            "savedOriginal": self.load_orig_state(profile.id),
        }

    def _try_extension_toggle(
        self,
        profile: DropdownProfile,
        state: RuntimeState,
        *,
        action: str,
        candidate_pids: list[int],
    ) -> dict[str, Any] | None:
        """Invoke typed ToggleDropdown D-Bus method on the opt-in SpawnKey GNOME extension."""
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            ext_obj = bus.get_object(EXTENSION_BUS_NAME, EXTENSION_OBJ_PATH)
            ext_iface = dbus.Interface(ext_obj, EXTENSION_IFACE)
            payload = self._build_extension_payload(
                profile,
                state,
                action=action,
                candidate_pids=candidate_pids,
            )
            raw = str(ext_iface.ToggleDropdown(json.dumps(payload, ensure_ascii=False)))
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("status") != "error":
                return parsed
        except Exception:
            pass
        return None

    def _try_shell_eval(self, script_js: str) -> str | None:
        """Attempt to execute GJS window control via `org.gnome.Shell.Eval` (unsafe_mode)."""
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            shell_obj = bus.get_object("org.gnome.Shell", "/org/gnome/Shell")
            shell_iface = dbus.Interface(shell_obj, "org.gnome.Shell")
            ok, result = shell_iface.Eval(script_js)
            if bool(ok):
                return str(result).strip('"')
        except Exception:
            pass
        return None

    def _launch_app(self, profile: DropdownProfile) -> int:
        argv, env = prepare_launch_argv_and_env(profile)
        proc = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid

    def _find_desktop_file(self, desktop_id: str) -> Path | None:
        target_name = desktop_id if desktop_id.endswith(".desktop") else f"{desktop_id}.desktop"
        for directory in default_application_dirs():
            candidate = directory / target_name
            if candidate.is_file():
                return candidate
        return None

    def _activate_desktop_app(self, profile: DropdownProfile) -> bool:
        """Best-effort activation of an already running DBus-activatable or .desktop app."""
        desktop_id = (profile.desktop_id or "").strip()
        if not desktop_id:
            return False

        app_id = desktop_id[:-8] if desktop_id.endswith(".desktop") else desktop_id
        if app_id and "." in app_id:
            try:
                import dbus  # type: ignore[import-not-found]

                bus = dbus.SessionBus()
                obj_path = "/" + app_id.replace(".", "/")
                app_obj = bus.get_object(app_id, obj_path)
                app_iface = dbus.Interface(app_obj, "org.freedesktop.Application")
                app_iface.Activate(dbus.Dictionary({}, signature="sv"))
                return True
            except Exception:
                pass

        gio_bin = shutil.which("gio")
        desktop_path = self._find_desktop_file(desktop_id)
        if gio_bin and desktop_path is not None:
            try:
                subprocess.Popen(
                    [gio_bin, "launch", str(desktop_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
        return False

    @staticmethod
    def _terminate_pids(pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def _collect_live_pids(self, profile: DropdownProfile, state: RuntimeState) -> list[int]:
        live_pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in live_pids and is_pid_alive_for_profile(state.pid, profile):
            live_pids.append(state.pid)
        return live_pids

    def toggle(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        with profile_run_guard(self.paths, profile.id, non_blocking=True, debounce_ms=0) as acquired:
            if not acquired:
                return state
            return self._toggle_locked(profile, state)

    def _toggle_locked(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        live_pids = self._collect_live_pids(profile, state)

        # 1. Opt-in GNOME Shell extension bridge (typed JSON D-Bus, no eval)
        ext_resp = self._try_extension_toggle(
            profile,
            state,
            action="toggle",
            candidate_pids=live_pids,
        )
        if ext_resp is not None:
            saved_orig = ext_resp.get("savedOriginal")
            if isinstance(saved_orig, dict):
                self.save_orig_state(profile.id, saved_orig)
            ext_status = ext_resp.get("status")
            ext_win_id = str(ext_resp.get("windowId") or state.window_id or "")
            ext_pid = ext_resp.get("pid")
            resolved_ext_pid = (
                live_pids[0]
                if live_pids
                else (int(ext_pid) if isinstance(ext_pid, int) and ext_pid > 0 else state.pid)
            )
            if ext_status == "hidden":
                if profile.mode is LifecycleMode.EPHEMERAL:
                    self.clear_orig_state(profile.id)
                    return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)
                if profile.mode is LifecycleMode.TRAY:
                    return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)
                return RuntimeState(
                    profile_id=profile.id,
                    pid=resolved_ext_pid,
                    window_id=ext_win_id,
                    visible=False,
                )
            if ext_status == "shown":
                return RuntimeState(
                    profile_id=profile.id,
                    pid=resolved_ext_pid,
                    window_id=ext_win_id,
                    visible=True,
                )
            if ext_status == "not_found":
                self._try_extension_toggle(
                    profile,
                    state,
                    action="watch_new",
                    candidate_pids=live_pids,
                )
                if profile.mode is LifecycleMode.TRAY:
                    sni_pid = sni_try_activate_tray_app(profile_match_tokens(profile))
                    if sni_pid is not None:
                        resolved = sni_pid if sni_pid > 0 else state.pid
                        return RuntimeState(profile_id=profile.id, pid=resolved, visible=True)
                new_pid = self._launch_app(profile)
                return RuntimeState(profile_id=profile.id, pid=new_pid, visible=True)

        # 2. Legacy development org.gnome.Shell.Eval bridge (unsafe_mode)
        monitor = self.get_active_monitor(profile)
        target = compute_geometry(profile, monitor)
        js = render_gnome_shell_eval_js(
            profile,
            target,
            action="toggle",
            candidate_pids=live_pids,
        )
        eval_res = self._try_shell_eval(js)
        if eval_res == "hidden":
            if profile.mode in (LifecycleMode.EPHEMERAL, LifecycleMode.TRAY):
                return RuntimeState(profile_id=profile.id, pid=None, visible=False)
            return RuntimeState(
                profile_id=profile.id,
                pid=live_pids[0] if live_pids else state.pid,
                visible=False,
            )
        if eval_res == "shown":
            return RuntimeState(
                profile_id=profile.id,
                pid=live_pids[0] if live_pids else state.pid,
                visible=True,
            )

        # 3. Stock locked-down GNOME Wayland fallback (lifecycle & SNI/D-Bus wake)
        if profile.mode is LifecycleMode.EPHEMERAL:
            if live_pids and state.visible:
                self._terminate_pids(live_pids)
                return RuntimeState(profile_id=profile.id, pid=None, visible=False)
            new_pid = self._launch_app(profile)
            return RuntimeState(profile_id=profile.id, pid=new_pid, visible=True)

        if profile.mode is LifecycleMode.TRAY:
            sni_pid = sni_try_activate_tray_app(profile_match_tokens(profile))
            if sni_pid is not None:
                resolved = sni_pid if sni_pid > 0 else state.pid
                return RuntimeState(
                    profile_id=profile.id,
                    pid=resolved,
                    visible=not state.visible,
                )
            new_pid = self._launch_app(profile)
            return RuntimeState(profile_id=profile.id, pid=new_pid, visible=True)

        # LifecycleMode.PERSISTENT
        if live_pids and state.visible:
            return RuntimeState(profile_id=profile.id, pid=live_pids[0], visible=False)
        if live_pids and not state.visible:
            self._activate_desktop_app(profile)
            return RuntimeState(profile_id=profile.id, pid=live_pids[0], visible=True)

        new_pid = self._launch_app(profile)
        return RuntimeState(profile_id=profile.id, pid=new_pid, visible=True)

    def hide_or_close(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        with profile_run_guard(self.paths, profile.id, non_blocking=False, debounce_ms=0):
            return self._hide_or_close_locked(profile, state)

    def _hide_or_close_locked(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        live_pids = self._collect_live_pids(profile, state)

        ext_resp = self._try_extension_toggle(
            profile,
            state,
            action="hide",
            candidate_pids=live_pids,
        )
        if ext_resp is None:
            monitor = self.get_active_monitor(profile)
            target = compute_geometry(profile, monitor)
            js = render_gnome_shell_eval_js(
                profile,
                target,
                action="hide",
                candidate_pids=live_pids,
            )
            self._try_shell_eval(js)

        if profile.mode is LifecycleMode.EPHEMERAL and live_pids:
            self._terminate_pids(live_pids)
            self.clear_orig_state(profile.id)
            return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)

        if profile.mode is LifecycleMode.TRAY:
            self.clear_orig_state(profile.id)
            return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)

        self.clear_orig_state(profile.id)
        if profile.mode is LifecycleMode.PERSISTENT:
            return RuntimeState(
                profile_id=profile.id,
                pid=live_pids[0] if live_pids else state.pid,
                window_id=state.window_id,
                visible=False,
            )

        return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)

    def diagnose(self) -> list[tuple[str, str, str]]:
        """Run non-destructive health checks for GNOME Wayland window control."""
        results: list[tuple[str, str, str]] = []

        monitors, primary_idx = self._query_mutter_monitors()
        if monitors:
            primary_name = monitors[primary_idx].name if 0 <= primary_idx < len(monitors) else ""
            results.append(
                (
                    "ok",
                    "Mutter DisplayConfig",
                    f"{len(monitors)} monitor(es) detectado(s) (primario: {primary_name or primary_idx}).",
                )
            )
        else:
            results.append(
                (
                    "warn",
                    "Mutter DisplayConfig",
                    "No se pudo consultar org.gnome.Mutter.DisplayConfig; usando geometría por defecto.",
                )
            )

        ping_ver = self._probe_extension_ping()
        if ping_ver is not None:
            results.append(
                (
                    "ok",
                    "Extensión SpawnKey GNOME",
                    f"Puente D-Bus activo (v{ping_ver}): posicionamiento, opacidad, animaciones y clic fuera disponibles.",
                )
            )
        elif self._is_extension_installed():
            results.append(("warn", "Extensión SpawnKey GNOME", _HINT_EXT_INSTALLED_INACTIVE))
        else:
            results.append(("warn", "Extensión SpawnKey GNOME", _HINT_EXT_NOT_INSTALLED))

        if ping_ver is None and self._probe_shell_eval_available():
            results.append(
                (
                    "ok",
                    "GNOME Shell Eval",
                    "Modo unsafe_mode activo: respaldo vía org.gnome.Shell.Eval disponible.",
                )
            )

        return results


__all__ = [
    "EXTENSION_UUID",
    "GnomeWaylandWindowBackend",
    "parse_mutter_logical_monitors",
    "render_gnome_shell_eval_js",
]
