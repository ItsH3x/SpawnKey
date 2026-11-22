"""GNOME Wayland window backend (Mutter DisplayConfig + opt-in Shell bridge / process fallback).

Technical note on GNOME Shell 45+ (Mutter Wayland):
- Standard Wayland `xdg-shell` does not permit clients to position top-level windows (`x, y`),
  strip server decorations of arbitrary processes, or force `keep_above` from outside the compositor.
- GNOME Shell 41+ disables unprivileged `org.gnome.Shell.Eval` by default unless an opt-in GNOME
  Shell extension (or development `unsafe_mode`) exposes a window control D-Bus interface.
- This backend queries real monitor geometries via `org.gnome.Mutter.DisplayConfig.GetCurrentState`,
  respects `profile.monitor_policy`, drives full dropdown geometry/opacity/flags when a GNOME Shell
  JS bridge (`io.github.spawnkey.GnomeShell` or `org.gnome.Shell.Eval`) is available, and falls back
  cleanly to zero-daemon process lifecycle management (`persistent`, `ephemeral`, `tray`) when running
  on stock locked-down GNOME Wayland.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any

from spawnkey.backends.base import (
    ENV_MARKER,
    BackendCapabilities,
    WindowBackend,
    build_launch_argv,
    find_profile_pids,
)
from spawnkey.core.geometry import compute_geometry, select_monitor_by_policy
from spawnkey.core.models import (
    DropdownProfile,
    LifecycleMode,
    MonitorRect,
    RuntimeState,
    WindowGeometry,
)
from spawnkey.core.storage import AppPaths


def parse_mutter_logical_monitors(logical_monitors: Any) -> tuple[list[MonitorRect], int]:
    """Parse `org.gnome.Mutter.DisplayConfig.GetCurrentState` logical_monitors structure."""
    monitors: list[MonitorRect] = []
    primary_idx = 0
    if not isinstance(logical_monitors, (list, tuple)):
        return monitors, primary_idx

    for idx, item in enumerate(logical_monitors):
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        x = int(item[0])
        y = int(item[1])
        scale = float(item[2]) if item[2] else 1.0
        is_primary = bool(item[4])
        connectors = item[5]
        conn_name = f"gnome-output-{idx}"
        if isinstance(connectors, (list, tuple)) and connectors:
            first_conn = connectors[0]
            if isinstance(first_conn, (list, tuple)) and first_conn:
                conn_name = str(first_conn[0])

        # Fallback width/height if mode dimensions are in properties or estimated from logical box
        width = int(item[6].get("width", 1920)) if len(item) > 6 and isinstance(item[6], dict) else 1920
        height = int(item[6].get("height", 1080)) if len(item) > 6 and isinstance(item[6], dict) else 1080
        if width <= 1 or height <= 1:
            continue
        if is_primary:
            primary_idx = len(monitors)
        _ = scale
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
        "pids": candidate_pids,
        "x": target.x,
        "y": target.y,
        "width": target.width,
        "height": target.height,
        "keepAbove": profile.keep_above,
        "showInTaskbar": profile.show_in_taskbar,
        "opacity": max(26, min(255, round(float(profile.opacity) * 255))),
    }
    cfg_json = json.dumps(payload, ensure_ascii=False)
    return f"""(function() {{
    const cfg = {cfg_json};
    const actors = global.get_window_actors ? global.get_window_actors() : [];
    function matches(win) {{
        if (!win) return false;
        const pid = win.get_pid ? win.get_pid() : -1;
        if (cfg.pids && cfg.pids.indexOf(pid) !== -1) return true;
        const wmClass = String(win.get_wm_class ? (win.get_wm_class() || "") : "").toLowerCase();
        const appId = String(win.get_gtk_application_id ? (win.get_gtk_application_id() || "") : "").toLowerCase();
        if (cfg.matchHint && (wmClass.indexOf(cfg.matchHint) !== -1 || appId.indexOf(cfg.matchHint) !== -1)) return true;
        return false;
    }}
    for (let i = actors.length - 1; i >= 0; i--) {{
        const actor = actors[i];
        const win = actor.get_meta_window ? actor.get_meta_window() : null;
        if (!matches(win)) continue;
        if (cfg.action === "hide" || (cfg.action === "toggle" && !win.minimized)) {{
            if (cfg.mode === "ephemeral" || cfg.mode === "tray") {{
                win.delete(global.get_current_time());
            }} else {{
                win.minimize();
            }}
            return "hidden";
        }}
        if (win.minimized) win.unminimize();
        if (win.get_maximized && win.get_maximized() !== 0 && win.unmaximize) win.unmaximize(3);
        win.move_resize_frame(true, cfg.x, cfg.y, cfg.width, cfg.height);
        if (cfg.keepAbove && win.make_above) win.make_above();
        if (win.stick) win.stick();
        if (actor.set_opacity) actor.set_opacity(cfg.opacity);
        win.activate(global.get_current_time());
        return "shown";
    }}
    return "not_found";
}})();"""


class GnomeWaylandWindowBackend(WindowBackend):
    """WindowBackend for GNOME on Wayland respecting zero resident daemon."""

    name = "gnome-wayland"
    capabilities = BackendCapabilities(
        supports_opacity=False,
        supports_corner_radius=False,
        supports_click_outside=False,
        supports_animations=False,
    )

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.from_env()

    def _query_mutter_monitors(self) -> tuple[list[MonitorRect], int]:
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            obj = bus.get_object("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig")
            iface = dbus.Interface(obj, "org.gnome.Mutter.DisplayConfig")
            state = iface.GetCurrentState()
            if isinstance(state, (list, tuple)) and len(state) >= 3:
                return parse_mutter_logical_monitors(state[2])
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

    def _try_shell_eval(self, script_js: str) -> str | None:
        """Attempt to execute GJS window control via opt-in SpawnKey extension or Shell.Eval."""
        try:
            import dbus  # type: ignore[import-not-found]

            bus = dbus.SessionBus()
            # 1. Opt-in minimal SpawnKey GNOME Shell extension D-Bus service
            try:
                ext_obj = bus.get_object("io.github.spawnkey.GnomeShell", "/io/github/spawnkey/GnomeShell")
                ext_iface = dbus.Interface(ext_obj, "io.github.spawnkey.GnomeShell")
                return str(ext_iface.EvalWindowScript(script_js))
            except Exception:
                pass

            # 2. Standard org.gnome.Shell.Eval (works when unsafe_mode / development mode is enabled)
            shell_obj = bus.get_object("org.gnome.Shell", "/org/gnome/Shell")
            shell_iface = dbus.Interface(shell_obj, "org.gnome.Shell")
            ok, result = shell_iface.Eval(script_js)
            if bool(ok):
                return str(result).strip('"')
        except Exception:
            pass
        return None

    def _launch_app(self, profile: DropdownProfile) -> int:
        argv = build_launch_argv(profile)
        env = dict(os.environ)
        env[ENV_MARKER] = profile.id
        env.setdefault("GTK_CSD", "0")
        proc = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid

    @staticmethod
    def _terminate_pids(pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def toggle(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        monitor = self.get_active_monitor(profile)
        target = compute_geometry(profile, monitor)
        live_pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in live_pids and Path(f"/proc/{state.pid}").exists():
            live_pids.append(state.pid)

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

        # Fallback without GNOME Shell extension: manage process lifecycle directly
        if live_pids and state.visible:
            if profile.mode is LifecycleMode.EPHEMERAL:
                self._terminate_pids(live_pids)
                return RuntimeState(profile_id=profile.id, pid=None, visible=False)
            return RuntimeState(profile_id=profile.id, pid=live_pids[0], visible=False)

        if not live_pids or profile.mode is LifecycleMode.TRAY:
            new_pid = self._launch_app(profile)
            return RuntimeState(profile_id=profile.id, pid=new_pid, visible=True)

        return RuntimeState(profile_id=profile.id, pid=live_pids[0], visible=True)

    def hide_or_close(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        monitor = self.get_active_monitor(profile)
        target = compute_geometry(profile, monitor)
        live_pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in live_pids and Path(f"/proc/{state.pid}").exists():
            live_pids.append(state.pid)

        js = render_gnome_shell_eval_js(
            profile,
            target,
            action="hide",
            candidate_pids=live_pids,
        )
        self._try_shell_eval(js)

        if profile.mode is LifecycleMode.EPHEMERAL and live_pids:
            self._terminate_pids(live_pids)
            return RuntimeState(profile_id=profile.id, pid=None, visible=False)

        return RuntimeState(
            profile_id=profile.id,
            pid=live_pids[0] if (live_pids and profile.mode is LifecycleMode.PERSISTENT) else None,
            visible=False,
        )
