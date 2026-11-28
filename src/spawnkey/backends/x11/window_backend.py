"""Universal X11 WindowBackend (EWMH/ICCCM via python-xlib with xdotool/wmctrl/xprop fallback)."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes.util
from dataclasses import dataclass
import math
import os
import re
import shutil
import signal
import subprocess
import time
from typing import Any, Iterator

from spawnkey.backends.base import (
    BackendCapabilities,
    WindowBackend,
    find_profile_pids,
    is_pid_alive_for_profile,
    prepare_launch_argv_and_env,
    profile_match_tokens,
    token_matches,
)
from spawnkey.backends.sni import sni_try_activate_tray_app
from spawnkey.core.geometry import (
    compute_geometry,
    select_monitor_by_policy,
    should_relocate_dropdown,
)
from spawnkey.core.models import (
    DropdownProfile,
    LifecycleMode,
    MonitorPolicy,
    MonitorRect,
    RuntimeState,
    WindowGeometry,
)
from spawnkey.core.storage import AppPaths, profile_run_guard

_NET_WM_STATE_REMOVE = 0
_NET_WM_STATE_ADD = 1
_ALL_DESKTOPS = 0xFFFFFFFF
_ICONIC_STATE = 3

_COMPOSITOR_NAMES = (
    "picom",
    "compton",
    "xcompmgr",
    "compiz",
    "xfwm4",
    "marco",
    "kwin_x11",
    "mutter",
    "gnome-shell",
    "cinnamon",
)

_IGNORED_WINDOW_TYPES = {
    "_NET_WM_WINDOW_TYPE_DESKTOP",
    "_NET_WM_WINDOW_TYPE_DOCK",
    "_NET_WM_WINDOW_TYPE_SPLASH",
}

_CORNER_RADIUS_SUPPORTED_HINT = "Recorte XShape sin antialiasing en X11"
_CORNER_RADIUS_MISSING_HINT = (
    "Instala python-xlib con soporte XShape o usa un compositor con redondeo"
)


_UNSET: Any = object()


@dataclass(frozen=True)
class _X11WindowInfo:
    """Minimal EWMH window metadata discovered via python-xlib or CLI tools."""

    window_id: str
    pid: int | None
    wm_classes: tuple[str, ...]
    title: str
    mapped: bool
    minimized: bool
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    active: bool | None = None


def parse_xrandr_outputs(raw_output: str) -> tuple[list[MonitorRect], int]:
    """Parse `xrandr --query` output into connected MonitorRects and primary index."""
    monitors: list[MonitorRect] = []
    primary_idx = 0

    for raw_line in (raw_output or "").splitlines():
        conn_match = re.match(r"^(\S+)\s+connected\b(.*)$", raw_line.strip())
        if not conn_match:
            continue
        output_name = conn_match.group(1)
        rest = conn_match.group(2)
        geom_match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", rest)
        if not geom_match:
            continue
        width, height, x, y = (int(group) for group in geom_match.groups())
        if width <= 1 or height <= 1:
            continue
        if re.search(r"\bprimary\b", rest):
            primary_idx = len(monitors)
        monitors.append(MonitorRect(x=x, y=y, width=width, height=height, name=output_name))

    return monitors, primary_idx


def _parse_wid(window_id: str) -> int | None:
    text = (window_id or "").strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _format_wid(wid_int: int) -> str:
    return f"0x{wid_int:08x}"


def _build_rounded_rectangles(
    width: int,
    height: int,
    radius: int,
) -> list[tuple[int, int, int, int]]:
    """Build YXBanded scanline rectangles for an XShape rounded-corner bounding region."""
    r = max(0, min(int(radius), width // 2, height // 2))
    if r <= 0:
        return [(0, 0, width, height)]

    rects: list[tuple[int, int, int, int]] = []
    for dy in range(r):
        cy = r - 0.5 - dy
        dx = int(math.floor(r - math.sqrt(max(0.0, r * r - cy * cy))))
        span = width - (2 * dx)
        if span > 0:
            rects.append((dx, dy, span, 1))

    mid_h = height - (2 * r)
    if mid_h > 0:
        rects.append((0, r, width, mid_h))

    for dy in range(r):
        cy = dy + 0.5
        dx = int(math.floor(r - math.sqrt(max(0.0, r * r - cy * cy))))
        span = width - (2 * dx)
        if span > 0:
            rects.append((dx, height - r + dy, span, 1))

    return rects


class _CapabilitiesDescriptor:
    """Return default capabilities on class access and live probed capabilities on instances."""

    def __init__(self, default: BackendCapabilities) -> None:
        self.default = default

    def __get__(
        self,
        instance: "X11WindowBackend | None",
        owner: type | None = None,
    ) -> BackendCapabilities:
        if instance is None:
            return self.default
        return instance._get_capabilities()


class X11WindowBackend(WindowBackend):
    """Universal X11 window backend with zero resident SpawnKey daemon."""

    name = "x11"
    capabilities = _CapabilitiesDescriptor(  # type: ignore[assignment]
        BackendCapabilities(
            supports_opacity=False,
            supports_corner_radius=False,
            supports_click_outside=False,
            supports_animations=False,
            corner_radius_hint=_CORNER_RADIUS_MISSING_HINT,
            backend_hint="",
        )
    )

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        debounce_ms: int = 0,
        use_xlib: bool = True,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        self.debounce_ms = max(0, int(debounce_ms))
        self.use_xlib = use_xlib
        self._caps_cache: BackendCapabilities | None = None

    def _get_capabilities(self) -> BackendCapabilities:
        if self._caps_cache is None:
            self._caps_cache = self._probe_capabilities()
        return self._caps_cache

    def refresh_capabilities(self) -> BackendCapabilities:
        """Re-probe X11 compositor (_NET_WM_CM_S0) and XShape availability."""
        self._caps_cache = None
        return self._get_capabilities()

    @staticmethod
    def _run_tool(cmd: list[str], *, timeout: float = 2.0) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    @contextmanager
    def _open_display(self) -> Iterator[Any | None]:
        """Open a transient Xlib Display for the current operation and close it immediately."""
        if not self.use_xlib or not os.environ.get("DISPLAY"):
            yield None
            return
        disp = None
        try:
            from Xlib import display as xdisplay  # type: ignore[import-not-found]

            disp = xdisplay.Display()
            yield disp
        except Exception:
            yield None
        finally:
            if disp is not None:
                try:
                    disp.close()
                except Exception:
                    pass

    def _probe_compositor(self) -> bool:
        with self._open_display() as disp:
            if disp is not None:
                try:
                    from Xlib import X  # type: ignore[import-not-found]

                    for atom_name in (f"_NET_WM_CM_S{disp.get_default_screen()}", "_NET_WM_CM_S0"):
                        owner = disp.get_selection_owner(disp.intern_atom(atom_name))
                        if owner != X.NONE and int(getattr(owner, "id", owner) or 0) != 0:
                            return True
                    return False
                except Exception:
                    pass

        xprop = shutil.which("xprop")
        if xprop and os.environ.get("DISPLAY"):
            proc = self._run_tool([xprop, "-root", "_NET_WM_CM_S0"])
            if proc and proc.returncode == 0:
                out = proc.stdout.strip()
                match = re.search(r"window id #\s*(0x[0-9a-fA-F]+|\d+)", out)
                if match:
                    val = _parse_wid(match.group(1))
                    return val is not None and val != 0
                return False

        pgrep = shutil.which("pgrep")
        if pgrep:
            proc = self._run_tool([pgrep, "-x", "|".join(_COMPOSITOR_NAMES)])
            if proc and proc.returncode == 0:
                return True
        return False

    def _probe_xshape(self) -> bool:
        module_ok = False
        try:
            from Xlib.ext import shape as _xshape  # type: ignore[import-not-found]

            _ = _xshape
            module_ok = True
        except Exception:
            module_ok = ctypes.util.find_library("Xext") is not None

        if not module_ok:
            return False

        with self._open_display() as disp:
            if disp is not None:
                try:
                    ext = disp.query_extension("SHAPE")
                    return bool(ext and getattr(ext, "present", True))
                except Exception:
                    return False
        return module_ok

    def _probe_capabilities(self) -> BackendCapabilities:
        has_compositor = self._probe_compositor()
        has_xshape = self._probe_xshape()
        corner_hint = (
            _CORNER_RADIUS_SUPPORTED_HINT if has_xshape else _CORNER_RADIUS_MISSING_HINT
        )
        backend_hint = (
            ""
            if has_compositor
            else "Sin compositor X11 activo (_NET_WM_CM_S0); la opacidad requiere picom/compositor."
        )
        return BackendCapabilities(
            supports_opacity=has_compositor,
            supports_corner_radius=has_xshape,
            supports_click_outside=False,
            supports_animations=False,
            corner_radius_hint=corner_hint,
            backend_hint=backend_hint,
        )

    def _query_monitors_xrandr_cli(self) -> tuple[list[MonitorRect], int]:
        xrandr = shutil.which("xrandr")
        if not xrandr:
            return [], 0
        proc = self._run_tool([xrandr, "--query"], timeout=3.0)
        if not proc or proc.returncode != 0:
            return [], 0
        return parse_xrandr_outputs(proc.stdout)

    def _query_monitors_xlib(self) -> tuple[list[MonitorRect], int]:
        monitors: list[MonitorRect] = []
        primary_idx = 0
        with self._open_display() as disp:
            if disp is None:
                return monitors, primary_idx
            try:
                from Xlib.ext import randr  # type: ignore[import-not-found]

                root = disp.screen().root
                res = randr.get_screen_resources(root)
                primary_out = None
                if hasattr(randr, "get_output_primary"):
                    primary_out = getattr(randr.get_output_primary(root), "output", None)
                for output in getattr(res, "outputs", ()):
                    info = randr.get_output_info(root, output, res.config_timestamp)
                    if getattr(info, "connection", 1) != 0 or not getattr(info, "crtc", 0):
                        continue
                    crtc = randr.get_crtc_info(root, info.crtc, res.config_timestamp)
                    w = int(getattr(crtc, "width", 0))
                    h = int(getattr(crtc, "height", 0))
                    if w <= 1 or h <= 1:
                        continue
                    name = getattr(info, "name", "")
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", errors="ignore")
                    if primary_out is not None and output == primary_out:
                        primary_idx = len(monitors)
                    monitors.append(
                        MonitorRect(
                            x=int(crtc.x),
                            y=int(crtc.y),
                            width=w,
                            height=h,
                            name=str(name or f"x11-output-{len(monitors)}"),
                        )
                    )
            except Exception:
                return [], 0
        return monitors, primary_idx

    def _query_cursor_pos(self) -> tuple[int, int] | None:
        with self._open_display() as disp:
            if disp is not None:
                try:
                    qp = disp.screen().root.query_pointer()
                    return (int(qp.root_x), int(qp.root_y))
                except Exception:
                    pass

        xdotool = shutil.which("xdotool")
        if xdotool and os.environ.get("DISPLAY"):
            proc = self._run_tool([xdotool, "getmouselocation", "--shell"], timeout=1.5)
            if proc and proc.returncode == 0:
                coords: dict[str, int] = {}
                for line in proc.stdout.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k in ("X", "Y"):
                            try:
                                coords[k] = int(v.strip())
                            except ValueError:
                                pass
                if "X" in coords and "Y" in coords:
                    return (coords["X"], coords["Y"])
        return None

    def _query_focused_monitor_index(self, monitors: list[MonitorRect]) -> int | None:
        if not monitors:
            return None
        with self._open_display() as disp:
            if disp is not None:
                try:
                    from Xlib import Xatom  # type: ignore[import-not-found]

                    root = disp.screen().root
                    active_prop = root.get_full_property(
                        disp.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW
                    )
                    if active_prop and active_prop.value and int(active_prop.value[0]) > 0:
                        win = disp.create_resource_object("window", int(active_prop.value[0]))
                        geom = win.get_geometry()
                        translated = win.translate_coords(root, 0, 0)
                        cx = int(translated.x) + (int(geom.width) // 2)
                        cy = int(translated.y) + (int(geom.height) // 2)
                        for idx, mon in enumerate(monitors):
                            if mon.x <= cx < (mon.x + mon.width) and mon.y <= cy < (mon.y + mon.height):
                                return idx
                except Exception:
                    pass
        return None

    def get_active_monitor(self, profile: DropdownProfile) -> MonitorRect:
        """Query connected X11 monitors and select one per profile.monitor_policy."""
        monitors, primary_idx = self._query_monitors_xrandr_cli()
        if not monitors:
            monitors, primary_idx = self._query_monitors_xlib()
        if not monitors:
            return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

        cursor_pos = self._query_cursor_pos()
        focused_idx = (
            self._query_focused_monitor_index(monitors)
            if profile.monitor_policy is MonitorPolicy.FOCUSED
            else None
        )
        return select_monitor_by_policy(
            monitors,
            profile.monitor_policy,
            primary_index=primary_idx,
            focused_index=focused_idx,
            cursor_pos=cursor_pos,
        )

    def _list_windows_xlib(self) -> list[_X11WindowInfo] | None:
        with self._open_display() as disp:
            if disp is None:
                return None
            try:
                from Xlib import X, Xatom  # type: ignore[import-not-found]

                root = disp.screen().root
                stack_atom = disp.intern_atom("_NET_CLIENT_LIST_STACKING")
                list_atom = disp.intern_atom("_NET_CLIENT_LIST")
                pid_atom = disp.intern_atom("_NET_WM_PID")
                state_atom = disp.intern_atom("_NET_WM_STATE")
                hidden_atom = disp.intern_atom("_NET_WM_STATE_HIDDEN")
                type_atom = disp.intern_atom("_NET_WM_WINDOW_TYPE")
                net_name_atom = disp.intern_atom("_NET_WM_NAME")
                utf8_atom = disp.intern_atom("UTF8_STRING")

                ignored_type_atoms = {disp.intern_atom(name) for name in _IGNORED_WINDOW_TYPES}

                active_wid: int | None = None
                try:
                    active_prop = root.get_full_property(
                        disp.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW
                    )
                    if active_prop and active_prop.value and int(active_prop.value[0]) > 0:
                        active_wid = int(active_prop.value[0])
                except Exception:
                    active_wid = None

                prop = root.get_full_property(stack_atom, Xatom.WINDOW)
                if prop is None or not prop.value:
                    prop = root.get_full_property(list_atom, Xatom.WINDOW)
                if prop and prop.value:
                    raw_ids = [int(wid) for wid in reversed(prop.value) if int(wid) > 0]
                else:
                    raw_ids = [int(child.id) for child in reversed(root.query_tree().children)]

                windows: list[_X11WindowInfo] = []
                for wid in raw_ids:
                    try:
                        win = disp.create_resource_object("window", wid)
                        type_prop = win.get_full_property(type_atom, Xatom.ATOM)
                        if type_prop and type_prop.value:
                            if all(int(a) in ignored_type_atoms for a in type_prop.value):
                                continue

                        pid_prop = win.get_full_property(pid_atom, Xatom.CARDINAL)
                        pid = int(pid_prop.value[0]) if (pid_prop and pid_prop.value) else None

                        wm_cls_raw = win.get_wm_class()
                        wm_classes = tuple(
                            part.strip() for part in (wm_cls_raw or ()) if part and part.strip()
                        )

                        title = ""
                        name_prop = win.get_full_property(net_name_atom, utf8_atom)
                        if name_prop and name_prop.value:
                            raw_val = name_prop.value
                            title = (
                                raw_val.decode("utf-8", errors="ignore")
                                if isinstance(raw_val, bytes)
                                else str(raw_val)
                            )
                        if not title:
                            title = str(win.get_wm_name() or "")

                        attrs = win.get_attributes()
                        mapped = int(attrs.map_state) == int(X.IsViewable)

                        state_prop = win.get_full_property(state_atom, Xatom.ATOM)
                        minimized = bool(
                            state_prop and state_prop.value and hidden_atom in state_prop.value
                        )

                        win_x: int | None = None
                        win_y: int | None = None
                        win_w: int | None = None
                        win_h: int | None = None
                        try:
                            geom = win.get_geometry()
                            translated = win.translate_coords(root, 0, 0)
                            gw = int(geom.width)
                            gh = int(geom.height)
                            if gw > 1 and gh > 1:
                                win_x = int(translated.x)
                                win_y = int(translated.y)
                                win_w = gw
                                win_h = gh
                        except Exception:
                            pass

                        windows.append(
                            _X11WindowInfo(
                                window_id=_format_wid(wid),
                                pid=pid,
                                wm_classes=wm_classes,
                                title=title,
                                mapped=mapped,
                                minimized=minimized,
                                x=win_x,
                                y=win_y,
                                width=win_w,
                                height=win_h,
                                active=(wid == active_wid) if active_wid is not None else None,
                            )
                        )
                    except Exception:
                        continue
                return windows
            except Exception:
                return None

    def _query_active_window_id_cli(self) -> int | None:
        xprop = shutil.which("xprop")
        if xprop:
            proc = self._run_tool([xprop, "-root", "_NET_ACTIVE_WINDOW"], timeout=1.5)
            if proc and proc.returncode == 0:
                match = re.search(r"window id #\s*(0x[0-9a-fA-F]+|\d+)", proc.stdout)
                if match:
                    val = _parse_wid(match.group(1))
                    return val if val is not None and val > 0 else None

        xdotool = shutil.which("xdotool")
        if xdotool:
            proc = self._run_tool([xdotool, "getactivewindow"], timeout=1.5)
            if proc and proc.returncode == 0:
                val = _parse_wid(proc.stdout.strip())
                return val if val is not None and val > 0 else None
        return None

    def _query_window_geometry_cli(self, wid_str: str) -> tuple[int, int, int, int] | None:
        xdotool = shutil.which("xdotool")
        if xdotool:
            proc = self._run_tool([xdotool, "getwindowgeometry", "--shell", wid_str], timeout=1.5)
            if proc and proc.returncode == 0:
                values: dict[str, int] = {}
                for line in proc.stdout.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k in ("X", "Y", "WIDTH", "HEIGHT"):
                            try:
                                values[k] = int(v.strip())
                            except ValueError:
                                pass
                if (
                    all(k in values for k in ("X", "Y", "WIDTH", "HEIGHT"))
                    and values["WIDTH"] > 1
                    and values["HEIGHT"] > 1
                ):
                    return (values["X"], values["Y"], values["WIDTH"], values["HEIGHT"])

        xwininfo = shutil.which("xwininfo")
        if xwininfo:
            proc = self._run_tool([xwininfo, "-id", wid_str], timeout=1.5)
            if proc and proc.returncode == 0:
                x_m = re.search(r"Absolute upper-left X:\s*(-?\d+)", proc.stdout)
                y_m = re.search(r"Absolute upper-left Y:\s*(-?\d+)", proc.stdout)
                w_m = re.search(r"Width:\s*(\d+)", proc.stdout)
                h_m = re.search(r"Height:\s*(\d+)", proc.stdout)
                if x_m and y_m and w_m and h_m:
                    w = int(w_m.group(1))
                    h = int(h_m.group(1))
                    if w > 1 and h > 1:
                        return (int(x_m.group(1)), int(y_m.group(1)), w, h)
        return None

    def _inspect_window_xprop(
        self,
        wid_str: str,
        *,
        active_wid: Any = _UNSET,
    ) -> _X11WindowInfo | None:
        xprop = shutil.which("xprop")
        if not xprop:
            return None
        proc = self._run_tool(
            [xprop, "-id", wid_str, "_NET_WM_PID", "WM_CLASS", "_NET_WM_STATE", "_NET_WM_NAME", "WM_NAME"],
            timeout=1.5,
        )
        if not proc or proc.returncode != 0:
            return None

        pid: int | None = None
        wm_classes: list[str] = []
        title = ""
        minimized = False
        for line in proc.stdout.splitlines():
            if line.startswith("_NET_WM_PID"):
                m = re.search(r"=\s*(\d+)", line)
                if m:
                    pid = int(m.group(1))
            elif line.startswith("WM_CLASS"):
                wm_classes.extend(re.findall(r'"([^"]+)"', line))
            elif line.startswith("_NET_WM_STATE"):
                if "_NET_WM_STATE_HIDDEN" in line:
                    minimized = True
            elif line.startswith("_NET_WM_NAME") or (not title and line.startswith("WM_NAME")):
                m = re.search(r'=\s*"(.*)"\s*$', line)
                if m:
                    title = m.group(1)

        geom = self._query_window_geometry_cli(wid_str)
        resolved_active = (
            self._query_active_window_id_cli() if active_wid is _UNSET else active_wid
        )
        wid_int = _parse_wid(wid_str)
        active = (
            wid_int == resolved_active
            if (wid_int is not None and resolved_active is not None)
            else None
        )

        return _X11WindowInfo(
            window_id=wid_str,
            pid=pid,
            wm_classes=tuple(c.strip() for c in wm_classes if c.strip()),
            title=title,
            mapped=not minimized,
            minimized=minimized,
            x=geom[0] if geom is not None else None,
            y=geom[1] if geom is not None else None,
            width=geom[2] if geom is not None else None,
            height=geom[3] if geom is not None else None,
            active=active,
        )

    def _list_windows_cli(self) -> list[_X11WindowInfo]:
        active_wid = self._query_active_window_id_cli()
        wmctrl = shutil.which("wmctrl")
        if wmctrl:
            proc_g = self._run_tool([wmctrl, "-lpGx"], timeout=2.0)
            if proc_g and proc_g.returncode == 0:
                windows: list[_X11WindowInfo] = []
                parse_ok = True
                for line in proc_g.stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split(None, 9)
                    if len(parts) < 9:
                        parse_ok = False
                        break
                    wid_int = _parse_wid(parts[0])
                    if wid_int is None:
                        continue
                    try:
                        pid = int(parts[2])
                        if pid <= 0:
                            pid = None
                    except ValueError:
                        pid = None
                    try:
                        gx = int(parts[3])
                        gy = int(parts[4])
                        gw = int(parts[5])
                        gh = int(parts[6])
                        win_x: int | None = gx if (gw > 1 and gh > 1) else None
                        win_y: int | None = gy if (gw > 1 and gh > 1) else None
                        win_w: int | None = gw if (gw > 1 and gh > 1) else None
                        win_h: int | None = gh if (gw > 1 and gh > 1) else None
                    except ValueError:
                        parse_ok = False
                        break
                    wm_classes = tuple(tok.strip() for tok in parts[7].split(".") if tok.strip())
                    title = parts[9] if len(parts) > 9 else ""
                    windows.append(
                        _X11WindowInfo(
                            window_id=_format_wid(wid_int),
                            pid=pid,
                            wm_classes=wm_classes,
                            title=title,
                            mapped=True,
                            minimized=False,
                            x=win_x,
                            y=win_y,
                            width=win_w,
                            height=win_h,
                            active=(wid_int == active_wid) if active_wid is not None else None,
                        )
                    )
                if parse_ok:
                    return list(reversed(windows))

            proc = self._run_tool([wmctrl, "-lpx"], timeout=2.0)
            if proc and proc.returncode == 0:
                windows = []
                for line in proc.stdout.splitlines():
                    parts = line.split(None, 5)
                    if len(parts) < 5:
                        continue
                    wid_int = _parse_wid(parts[0])
                    if wid_int is None:
                        continue
                    wid_str = _format_wid(wid_int)
                    try:
                        pid = int(parts[2])
                        if pid <= 0:
                            pid = None
                    except ValueError:
                        pid = None
                    wm_classes = tuple(tok.strip() for tok in parts[3].split(".") if tok.strip())
                    title = parts[5] if len(parts) > 5 else ""
                    windows.append(
                        _X11WindowInfo(
                            window_id=wid_str,
                            pid=pid,
                            wm_classes=wm_classes,
                            title=title,
                            mapped=True,
                            minimized=False,
                            active=(wid_int == active_wid) if active_wid is not None else None,
                        )
                    )
                return list(reversed(windows))

        xdotool = shutil.which("xdotool")
        if xdotool:
            proc = self._run_tool([xdotool, "search", ""], timeout=2.0)
            if proc and proc.returncode == 0:
                windows = []
                for line in reversed(proc.stdout.splitlines()):
                    wid_int = _parse_wid(line.strip())
                    if wid_int is None or wid_int <= 0:
                        continue
                    info = self._inspect_window_xprop(_format_wid(wid_int), active_wid=active_wid)
                    if info is not None:
                        windows.append(info)
                return windows

        xprop = shutil.which("xprop")
        if xprop:
            proc = self._run_tool([xprop, "-root", "_NET_CLIENT_LIST"], timeout=2.0)
            if proc and proc.returncode == 0:
                windows = []
                for tok in reversed(re.findall(r"0x[0-9a-fA-F]+", proc.stdout)):
                    wid_int = _parse_wid(tok)
                    if wid_int is None or wid_int <= 0:
                        continue
                    info = self._inspect_window_xprop(_format_wid(wid_int), active_wid=active_wid)
                    if info is not None:
                        windows.append(info)
                return windows

        return []

    def _list_windows(self) -> list[_X11WindowInfo]:
        xlib_windows = self._list_windows_xlib()
        if xlib_windows is not None:
            return xlib_windows
        return self._list_windows_cli()

    def _find_profile_window(
        self,
        profile: DropdownProfile,
        state: RuntimeState,
        live_pids: list[int],
        *,
        allow_class_fallback: bool = True,
    ) -> _X11WindowInfo | None:
        windows = self._list_windows()
        if not windows:
            return None

        target_wid = _parse_wid(state.window_id)
        if target_wid is not None:
            for win in windows:
                if _parse_wid(win.window_id) == target_wid:
                    return win

        if live_pids:
            pid_set = set(live_pids)
            for win in windows:
                if win.pid is not None and win.pid in pid_set:
                    return win

        if allow_class_fallback:
            hints = profile_match_tokens(profile)
            for win in windows:
                candidates = [*win.wm_classes, win.title]
                if any(
                    token_matches(cand, hint)
                    for cand in candidates
                    if cand
                    for hint in hints
                ):
                    return win

        return None

    def _send_ewmh_client_message(
        self,
        disp: Any,
        win: Any,
        atom_name: str,
        data: list[int],
    ) -> None:
        from Xlib import X  # type: ignore[import-not-found]
        from Xlib.protocol import event  # type: ignore[import-not-found]

        root = disp.screen().root
        padded = (list(data) + [0, 0, 0, 0, 0])[:5]
        ev = event.ClientMessage(
            window=win,
            client_type=disp.intern_atom(atom_name),
            data=(32, padded),
        )
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)

    def _apply_motif_border(self, wid_str: str, *, borderless: bool) -> None:
        decor = 0 if borderless else 1
        wid_int = _parse_wid(wid_str)
        if wid_int is None:
            return

        with self._open_display() as disp:
            if disp is not None:
                try:
                    win = disp.create_resource_object("window", wid_int)
                    motif_atom = disp.intern_atom("_MOTIF_WM_HINTS")
                    win.change_property(motif_atom, motif_atom, 32, [2, 0, decor, 0, 0])
                    disp.sync()
                    return
                except Exception:
                    pass

        xprop = shutil.which("xprop")
        if xprop:
            self._run_tool(
                [
                    xprop,
                    "-id",
                    wid_str,
                    "-f",
                    "_MOTIF_WM_HINTS",
                    "32c",
                    "-set",
                    "_MOTIF_WM_HINTS",
                    f"0x2, 0x0, 0x{decor}, 0x0, 0x0",
                ],
                timeout=1.5,
            )

    def _apply_i3_rules(self, wid_str: str, target: WindowGeometry) -> None:
        if not os.environ.get("I3SOCK"):
            return
        i3_msg = shutil.which("i3-msg")
        if not i3_msg:
            return
        wid_int = _parse_wid(wid_str)
        if wid_int is None:
            return
        cmd = (
            f"[id={wid_int}] floating enable, border none, "
            f"move position {target.x} px {target.y} px, "
            f"resize set {target.width} px {target.height} px"
        )
        self._run_tool([i3_msg, cmd], timeout=1.5)

    def _apply_dropdown_window(
        self,
        wid_str: str,
        profile: DropdownProfile,
        target: WindowGeometry,
    ) -> None:
        wid_int = _parse_wid(wid_str)
        if wid_int is None:
            return

        applied_xlib = False
        with self._open_display() as disp:
            if disp is not None:
                try:
                    from Xlib import X, Xatom  # type: ignore[import-not-found]

                    win = disp.create_resource_object("window", wid_int)
                    win.map()

                    # 1. Remove maximized state before applying exact geometry
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_WM_STATE",
                        [
                            _NET_WM_STATE_REMOVE,
                            disp.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                            disp.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ"),
                            1,
                            0,
                        ],
                    )

                    # 2. Enforce borderless via _MOTIF_WM_HINTS
                    motif_atom = disp.intern_atom("_MOTIF_WM_HINTS")
                    win.change_property(motif_atom, motif_atom, 32, [2, 0, 0, 0, 0])

                    # 3. Move & resize
                    win.configure(
                        x=target.x,
                        y=target.y,
                        width=target.width,
                        height=target.height,
                    )
                    # Bitmask: gravity=Static(10) | X(8) | Y(9) | W(10) | H(11) | source=1(12)
                    flags = (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12) | 10
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_MOVERESIZE_WINDOW",
                        [flags, target.x, target.y, target.width, target.height],
                    )

                    # 4. Keep above, taskbar/pager visibility, and sticky across desktops
                    above_action = _NET_WM_STATE_ADD if profile.keep_above else _NET_WM_STATE_REMOVE
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_WM_STATE",
                        [above_action, disp.intern_atom("_NET_WM_STATE_ABOVE"), 0, 1, 0],
                    )

                    skip_action = _NET_WM_STATE_REMOVE if profile.show_in_taskbar else _NET_WM_STATE_ADD
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_WM_STATE",
                        [
                            skip_action,
                            disp.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
                            disp.intern_atom("_NET_WM_STATE_SKIP_PAGER"),
                            1,
                            0,
                        ],
                    )

                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_WM_STATE",
                        [_NET_WM_STATE_ADD, disp.intern_atom("_NET_WM_STATE_STICKY"), 0, 1, 0],
                    )
                    win.change_property(
                        disp.intern_atom("_NET_WM_DESKTOP"),
                        Xatom.CARDINAL,
                        32,
                        [_ALL_DESKTOPS],
                    )
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_WM_DESKTOP",
                        [_ALL_DESKTOPS, 1, 0, 0, 0],
                    )

                    # 5. Opacity via _NET_WM_WINDOW_OPACITY
                    opacity_atom = disp.intern_atom("_NET_WM_WINDOW_OPACITY")
                    opacity_val = max(0, min(0xFFFFFFFF, round(float(profile.opacity) * 0xFFFFFFFF)))
                    if opacity_val < 0xFFFFFFFF:
                        win.change_property(opacity_atom, Xatom.CARDINAL, 32, [opacity_val])
                    else:
                        win.delete_property(opacity_atom)

                    # 6. Optional XShape rounded-corner clip
                    if hasattr(win, "shape_rectangles") and hasattr(win, "shape_mask"):
                        try:
                            from Xlib.ext import shape  # type: ignore[import-not-found]

                            if profile.corner_radius > 0:
                                rects = _build_rounded_rectangles(
                                    target.width,
                                    target.height,
                                    profile.corner_radius,
                                )
                                win.shape_rectangles(
                                    shape.SO.Set,
                                    shape.SK.Bounding,
                                    X.YXBanded,
                                    0,
                                    0,
                                    rects,
                                )
                            else:
                                win.shape_mask(shape.SO.Set, shape.SK.Bounding, 0, 0, X.NONE)
                        except Exception:
                            pass

                    # 7. Raise and activate
                    win.raise_window()
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "_NET_ACTIVE_WINDOW",
                        [1, X.CurrentTime, 0, 0, 0],
                    )
                    disp.sync()
                    applied_xlib = True
                except Exception:
                    applied_xlib = False

        if not applied_xlib:
            self._apply_dropdown_window_cli(wid_str, profile, target)

        self._apply_i3_rules(wid_str, target)

    def _apply_dropdown_window_cli(
        self,
        wid_str: str,
        profile: DropdownProfile,
        target: WindowGeometry,
    ) -> None:
        wmctrl = shutil.which("wmctrl")
        xdotool = shutil.which("xdotool")
        xprop = shutil.which("xprop")

        if xdotool:
            self._run_tool([xdotool, "windowmap", wid_str], timeout=1.5)
        if wmctrl:
            self._run_tool(
                [wmctrl, "-ir", wid_str, "-b", "remove,maximized_vert,maximized_horz"],
                timeout=1.5,
            )

        self._apply_motif_border(wid_str, borderless=True)

        if xdotool:
            self._run_tool(
                [
                    xdotool,
                    "windowmove",
                    wid_str,
                    str(target.x),
                    str(target.y),
                    "windowsize",
                    wid_str,
                    str(target.width),
                    str(target.height),
                ],
                timeout=1.5,
            )
        elif wmctrl:
            self._run_tool(
                [
                    wmctrl,
                    "-ir",
                    wid_str,
                    "-e",
                    f"0,{target.x},{target.y},{target.width},{target.height}",
                ],
                timeout=1.5,
            )

        if wmctrl:
            above_op = "add" if profile.keep_above else "remove"
            skip_op = "remove" if profile.show_in_taskbar else "add"
            self._run_tool([wmctrl, "-ir", wid_str, "-b", f"{above_op},above"], timeout=1.5)
            self._run_tool(
                [wmctrl, "-ir", wid_str, "-b", f"{skip_op},skip_taskbar,skip_pager"],
                timeout=1.5,
            )
            self._run_tool([wmctrl, "-ir", wid_str, "-b", "add,sticky"], timeout=1.5)
            self._run_tool([wmctrl, "-ir", wid_str, "-t", "-1"], timeout=1.5)

        if xprop:
            opacity_val = max(0, min(0xFFFFFFFF, round(float(profile.opacity) * 0xFFFFFFFF)))
            if opacity_val < 0xFFFFFFFF:
                self._run_tool(
                    [
                        xprop,
                        "-id",
                        wid_str,
                        "-f",
                        "_NET_WM_WINDOW_OPACITY",
                        "32c",
                        "-set",
                        "_NET_WM_WINDOW_OPACITY",
                        str(opacity_val),
                    ],
                    timeout=1.5,
                )
            else:
                self._run_tool([xprop, "-id", wid_str, "-remove", "_NET_WM_WINDOW_OPACITY"], timeout=1.5)

        if xdotool:
            self._run_tool([xdotool, "windowactivate", wid_str], timeout=1.5)
        elif wmctrl:
            self._run_tool([wmctrl, "-ia", wid_str], timeout=1.5)

    def _minimize_window(self, wid_str: str) -> None:
        wid_int = _parse_wid(wid_str)
        if wid_int is None:
            return
        with self._open_display() as disp:
            if disp is not None:
                try:
                    win = disp.create_resource_object("window", wid_int)
                    self._send_ewmh_client_message(
                        disp,
                        win,
                        "WM_CHANGE_STATE",
                        [_ICONIC_STATE, 0, 0, 0, 0],
                    )
                    disp.sync()
                    return
                except Exception:
                    pass

        xdotool = shutil.which("xdotool")
        if xdotool:
            self._run_tool([xdotool, "windowminimize", wid_str], timeout=1.5)

    def _close_window(self, wid_str: str, *, restore_borders: bool = False) -> None:
        if restore_borders:
            self._apply_motif_border(wid_str, borderless=False)

        wid_int = _parse_wid(wid_str)
        if wid_int is not None:
            with self._open_display() as disp:
                if disp is not None:
                    try:
                        from Xlib import X  # type: ignore[import-not-found]

                        win = disp.create_resource_object("window", wid_int)
                        self._send_ewmh_client_message(
                            disp,
                            win,
                            "_NET_CLOSE_WINDOW",
                            [X.CurrentTime, 1, 0, 0, 0],
                        )
                        disp.sync()
                        return
                    except Exception:
                        pass

        wmctrl = shutil.which("wmctrl")
        if wmctrl:
            proc = self._run_tool([wmctrl, "-ic", wid_str], timeout=1.5)
            if proc and proc.returncode == 0:
                return
        xdotool = shutil.which("xdotool")
        if xdotool:
            self._run_tool([xdotool, "windowclose", wid_str], timeout=1.5)

    def _launch_app(self, profile: DropdownProfile) -> int:
        argv, env = prepare_launch_argv_and_env(profile)
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

    def _poll_for_window(
        self,
        profile: DropdownProfile,
        state: RuntimeState,
        initial_pids: list[int],
    ) -> _X11WindowInfo | None:
        timeout_ms = min(int(profile.startup_timeout_ms), 5000)
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while True:
            pids = list(initial_pids)
            for found_pid in find_profile_pids(profile.id):
                if found_pid not in pids:
                    pids.append(found_pid)
            win = self._find_profile_window(
                profile,
                state,
                pids,
                allow_class_fallback=True,
            )
            if win is not None:
                return win
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def _collect_live_pids(self, profile: DropdownProfile, state: RuntimeState) -> list[int]:
        live_pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in live_pids and is_pid_alive_for_profile(state.pid, profile):
            live_pids.append(state.pid)
        return live_pids

    def toggle(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        """Toggle visibility or lifecycle of an X11 dropdown window."""
        with profile_run_guard(
            self.paths,
            profile.id,
            debounce_ms=0,
            non_blocking=True,
        ) as acquired:
            if not acquired:
                return state
            return self._toggle_locked(profile, state)

    def _toggle_locked(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        monitor = self.get_active_monitor(profile)
        target = compute_geometry(profile, monitor)
        live_pids = self._collect_live_pids(profile, state)

        existing = self._find_profile_window(
            profile,
            state,
            live_pids,
            allow_class_fallback=bool(
                state.window_id or state.pid is not None or state.visible or profile.mode is LifecycleMode.TRAY
            ),
        )

        if existing is not None:
            resolved_pid = existing.pid or (live_pids[0] if live_pids else state.pid)
            if state.visible:
                win_rect = (
                    (existing.x, existing.y, existing.width, existing.height)
                    if (
                        existing.x is not None
                        and existing.y is not None
                        and existing.width is not None
                        and existing.height is not None
                    )
                    else None
                )
                if should_relocate_dropdown(
                    profile,
                    monitor,
                    win_rect=win_rect,
                    is_focused=existing.active,
                ):
                    self._apply_dropdown_window(existing.window_id, profile, target)
                    return RuntimeState(
                        profile_id=profile.id,
                        pid=resolved_pid,
                        window_id=existing.window_id,
                        visible=True,
                    )
                if profile.mode in (LifecycleMode.EPHEMERAL, LifecycleMode.TRAY):
                    self._close_window(
                        existing.window_id,
                        restore_borders=(profile.mode is LifecycleMode.TRAY),
                    )
                    return RuntimeState(
                        profile_id=profile.id,
                        pid=None,
                        window_id="",
                        visible=False,
                    )
                self._minimize_window(existing.window_id)
                return RuntimeState(
                    profile_id=profile.id,
                    pid=resolved_pid,
                    window_id=existing.window_id,
                    visible=False,
                )

            self._apply_dropdown_window(existing.window_id, profile, target)
            return RuntimeState(
                profile_id=profile.id,
                pid=resolved_pid,
                window_id=existing.window_id,
                visible=True,
            )

        launched_pid: int | None = None
        if profile.mode is LifecycleMode.TRAY:
            sni_pid = sni_try_activate_tray_app(profile_match_tokens(profile))
            if sni_pid is not None:
                launched_pid = sni_pid if sni_pid > 0 else None
            else:
                launched_pid = self._launch_app(profile)
        else:
            launched_pid = self._launch_app(profile)

        seed_pids = list(live_pids)
        if launched_pid and launched_pid > 0 and launched_pid not in seed_pids:
            seed_pids.append(launched_pid)

        appeared = self._poll_for_window(profile, state, seed_pids)
        if appeared is not None:
            self._apply_dropdown_window(appeared.window_id, profile, target)
            resolved_pid = appeared.pid or launched_pid or (seed_pids[0] if seed_pids else None)
            return RuntimeState(
                profile_id=profile.id,
                pid=resolved_pid,
                window_id=appeared.window_id,
                visible=True,
            )

        return RuntimeState(
            profile_id=profile.id,
            pid=launched_pid,
            window_id="",
            visible=True,
        )

    def hide_or_close(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        """Minimize (persistent) or close (ephemeral/tray, restoring borders on tray)."""
        with profile_run_guard(
            self.paths,
            profile.id,
            debounce_ms=0,
            non_blocking=False,
        ):
            live_pids = self._collect_live_pids(profile, state)
            existing = self._find_profile_window(
                profile,
                state,
                live_pids,
                allow_class_fallback=True,
            )

            if profile.mode in (LifecycleMode.EPHEMERAL, LifecycleMode.TRAY):
                if existing is not None:
                    self._close_window(
                        existing.window_id,
                        restore_borders=(profile.mode is LifecycleMode.TRAY),
                    )
                elif profile.mode is LifecycleMode.EPHEMERAL:
                    for pid in live_pids:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
                return RuntimeState(
                    profile_id=profile.id,
                    pid=None,
                    window_id="",
                    visible=False,
                )

            resolved_pid = live_pids[0] if live_pids else state.pid
            if existing is not None:
                self._minimize_window(existing.window_id)
                resolved_pid = existing.pid or resolved_pid
                return RuntimeState(
                    profile_id=profile.id,
                    pid=resolved_pid,
                    window_id=existing.window_id,
                    visible=False,
                )
            return RuntimeState(
                profile_id=profile.id,
                pid=resolved_pid,
                window_id=state.window_id,
                visible=False,
            )

    def diagnose(self) -> list[tuple[str, str, str]]:
        """Report health of DISPLAY, xrandr, python-xlib, CLI tools, and X11 compositor."""
        results: list[tuple[str, str, str]] = []
        display_env = os.environ.get("DISPLAY", "").strip()
        results.append(
            (
                "ok" if display_env else "error",
                "DISPLAY",
                display_env or "Variable DISPLAY no definida.",
            )
        )

        xrandr_path = shutil.which("xrandr")
        results.append(
            (
                "ok" if xrandr_path else "warn",
                "xrandr",
                xrandr_path or "No encontrado en PATH; se usará fallback RandR si python-xlib está disponible.",
            )
        )

        try:
            import Xlib  # type: ignore[import-not-found]  # noqa: F401

            xlib_ok = True
        except Exception:
            xlib_ok = False
        results.append(
            (
                "ok" if xlib_ok else "warn",
                "python-xlib",
                "Disponible" if xlib_ok else "No instalado; usando herramientas CLI X11 como respaldo.",
            )
        )

        cli_tools = [name for name in ("xdotool", "wmctrl", "xprop") if shutil.which(name)]
        control_tools = [name for name in ("xdotool", "wmctrl") if name in cli_tools]
        if xlib_ok or control_tools:
            cli_level = "ok" if cli_tools else "ok"
            cli_detail = (
                f"Disponibles: {', '.join(cli_tools)}"
                if cli_tools
                else "No requeridas (python-xlib disponible)."
            )
        else:
            cli_level = "error"
            cli_detail = (
                f"Solo {', '.join(cli_tools)} disponible(s); instala python-xlib o xdotool/wmctrl para posicionar y ocultar ventanas en X11."
                if cli_tools
                else "No se encontró python-xlib, xdotool ni wmctrl; instala python-xlib o xdotool/wmctrl para controlar ventanas en X11."
            )
        results.append((cli_level, "Herramientas CLI X11", cli_detail))

        caps = self.refresh_capabilities()
        results.append(
            (
                "ok" if caps.supports_opacity else "warn",
                "Compositor (_NET_WM_CM_S0)",
                "Compositor X11 activo (opacidad soportada)."
                if caps.supports_opacity
                else "Sin compositor X11 activo; la opacidad no se aplicará.",
            )
        )
        results.append(
            (
                "ok" if caps.supports_corner_radius else "warn",
                "XShape",
                caps.corner_radius_hint,
            )
        )
        return results
