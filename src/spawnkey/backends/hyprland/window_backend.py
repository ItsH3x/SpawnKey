"""Hyprland Wayland window backend driven by Hyprland UNIX socket IPC with hyprctl fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import time
from typing import Any

from spawnkey.backends.base import (
    BackendCapabilities,
    BackendError,
    DiagnosticItem,
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
)
from spawnkey.core.storage import AppPaths, profile_run_guard


def _resolve_hypr_socket() -> Path | None:
    """Resolve the active Hyprland .socket.sock path if available."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
    if not sig:
        return None
    candidates: list[Path] = []
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg_runtime:
        candidates.append(Path(xdg_runtime) / "hypr" / sig / ".socket.sock")
    candidates.append(Path("/tmp") / "hypr" / sig / ".socket.sock")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _hyprctl_cli(cmd: str, *, timeout: float = 2.0) -> str:
    """Execute a Hyprland IPC command via the `hyprctl` CLI fallback."""
    hyprctl = shutil.which("hyprctl")
    if not hyprctl:
        raise BackendError("No se encontró 'hyprctl' ni el socket IPC de Hyprland.")

    text = cmd.strip()
    if text.startswith("j/"):
        argv = [hyprctl, "-j", *shlex.split(text[2:])]
    elif text.startswith("dispatch "):
        rest = text[len("dispatch ") :].strip()
        parts = rest.split(None, 1)
        argv = [hyprctl, "dispatch", *parts]
    elif text.startswith("keyword "):
        rest = text[len("keyword ") :].strip()
        parts = rest.split(None, 1)
        argv = [hyprctl, "keyword", *parts]
    else:
        argv = [hyprctl, *shlex.split(text)]

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError(f"Error ejecutando hyprctl ({text}): {exc}") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise BackendError(f"hyprctl falló ({text}): {err}")
    return proc.stdout.strip()


def _hypr_ipc(cmd: str, *, timeout: float = 2.0) -> str:
    """Send a command over Hyprland's UNIX socket IPC, falling back to `hyprctl`."""
    sock_path = _resolve_hypr_socket()
    if sock_path is not None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(str(sock_path))
                sock.sendall(cmd.encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            return b"".join(chunks).decode("utf-8", errors="replace").strip()
        except OSError:
            pass

    return _hyprctl_cli(cmd, timeout=timeout)


def _hypr_json(endpoint: str, *, timeout: float = 2.0) -> Any:
    """Query a JSON endpoint from Hyprland IPC (e.g. 'monitors', 'clients')."""
    raw = _hypr_ipc(f"j/{endpoint}", timeout=timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BackendError(f"Respuesta JSON inválida de Hyprland (j/{endpoint}): {exc}") from exc


def parse_hyprland_monitors(
    raw_monitors: list[dict[str, Any]],
    cursor_pos: tuple[int, int] | None = None,
) -> tuple[list[MonitorRect], int]:
    """Parse `hyprctl -j monitors` entries into logical MonitorRects and focused index."""
    monitors: list[MonitorRect] = []
    focused_idx: int | None = None

    for idx, mon in enumerate(raw_monitors):
        if not isinstance(mon, dict) or mon.get("disabled"):
            continue
        raw_w = int(mon.get("width", 0))
        raw_h = int(mon.get("height", 0))
        scale = float(mon.get("scale") or 1.0)
        if scale <= 0:
            scale = 1.0

        w = round(raw_w / scale)
        h = round(raw_h / scale)
        if int(mon.get("transform", 0)) in (1, 3, 5, 7):
            w, h = h, w
        if w <= 1 or h <= 1:
            continue

        x = int(mon.get("x", 0))
        y = int(mon.get("y", 0))
        name = str(mon.get("name") or f"hypr-output-{idx}")
        if mon.get("focused"):
            focused_idx = len(monitors)
        monitors.append(MonitorRect(x=x, y=y, width=w, height=h, name=name))

    if focused_idx is None and cursor_pos is not None:
        cx, cy = cursor_pos
        for i, rect in enumerate(monitors):
            if rect.x <= cx < (rect.x + rect.width) and rect.y <= cy < (rect.y + rect.height):
                focused_idx = i
                break

    return monitors, (focused_idx if focused_idx is not None else 0)


def _should_relocate_client(
    profile: DropdownProfile,
    client: dict[str, Any],
    target_monitor: MonitorRect,
) -> bool:
    """Return True when a visible Hyprland client should be relocated or refocused."""
    win_rect: tuple[int, int, int, int] | None = None
    at = client.get("at")
    size = client.get("size")
    if (
        isinstance(at, (list, tuple))
        and isinstance(size, (list, tuple))
        and len(at) >= 2
        and len(size) >= 2
    ):
        try:
            w = int(size[0])
            h = int(size[1])
            if w > 1 and h > 1:
                win_rect = (int(at[0]), int(at[1]), w, h)
        except (TypeError, ValueError):
            win_rect = None

    is_focused: bool | None = None
    if "focusHistoryID" in client and client["focusHistoryID"] is not None:
        try:
            is_focused = int(client["focusHistoryID"]) == 0
        except (TypeError, ValueError):
            is_focused = None

    return should_relocate_dropdown(
        profile,
        target_monitor,
        win_rect=win_rect,
        is_focused=is_focused,
    )


class HyprlandWindowBackend(WindowBackend):
    """WindowBackend for Hyprland using native UNIX socket IPC."""

    name = "hyprland"
    capabilities = BackendCapabilities(
        supports_opacity=True,
        supports_corner_radius=True,
        supports_click_outside=False,
        supports_animations=False,
    )

    def __init__(self, paths: AppPaths | None = None, *, debounce_ms: int = 0) -> None:
        self.paths = paths or AppPaths.from_env()
        self.debounce_ms = max(0, int(debounce_ms))

    def _query_cursor_pos(self) -> tuple[int, int] | None:
        try:
            data = _hypr_json("cursorpos")
            if isinstance(data, dict) and "x" in data and "y" in data:
                return (round(float(data["x"])), round(float(data["y"])))
        except Exception:
            pass
        return None

    def get_active_monitor(self, profile: DropdownProfile) -> MonitorRect:
        """Return the active monitor rectangle honoring profile.monitor_policy."""
        try:
            raw_monitors = _hypr_json("monitors")
            if isinstance(raw_monitors, list) and raw_monitors:
                cursor_pos = self._query_cursor_pos()
                monitors, focused_idx = parse_hyprland_monitors(raw_monitors, cursor_pos=cursor_pos)
                if monitors:
                    primary_idx = next(
                        (i for i, m in enumerate(monitors) if m.x == 0 and m.y == 0),
                        0,
                    )
                    return select_monitor_by_policy(
                        monitors,
                        profile.monitor_policy,
                        primary_index=primary_idx,
                        focused_index=focused_idx,
                        cursor_pos=cursor_pos,
                    )
        except Exception:
            pass
        return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    @staticmethod
    def _special_workspace_name(profile: DropdownProfile) -> str:
        return f"special:spawnkey_{profile.id}"

    def _collect_live_pids(self, profile: DropdownProfile, state: RuntimeState) -> list[int]:
        pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in pids and is_pid_alive_for_profile(state.pid, profile):
            pids.append(state.pid)
        return pids

    @staticmethod
    def _find_matching_client(
        clients: list[dict[str, Any]],
        profile: DropdownProfile,
        state: RuntimeState,
        live_pids: list[int],
    ) -> dict[str, Any] | None:
        if state.window_id:
            target_addr = state.window_id.strip().lower()
            for client in clients:
                if str(client.get("address") or "").strip().lower() == target_addr:
                    return client

        if live_pids:
            pid_set = set(live_pids)
            for client in clients:
                try:
                    cpid = int(client.get("pid") or 0)
                except (TypeError, ValueError):
                    cpid = 0
                if cpid in pid_set:
                    return client

        tokens = profile_match_tokens(profile)
        if tokens:
            for client in clients:
                fields = (
                    str(client.get("class") or ""),
                    str(client.get("initialClass") or ""),
                    str(client.get("title") or ""),
                )
                if any(token_matches(field, tok) for field in fields if field for tok in tokens):
                    return client

        return None

    def _is_client_visible(self, client: dict[str, Any], profile: DropdownProfile) -> bool:
        ws = client.get("workspace")
        ws_name = str(ws.get("name") or "") if isinstance(ws, dict) else ""
        if ws_name == self._special_workspace_name(profile) or ws_name.startswith("special:"):
            return False
        if client.get("hidden"):
            return False
        return True

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

    @staticmethod
    def _terminate_pids(pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def _apply_show_sequence(
        self,
        profile: DropdownProfile,
        client: dict[str, Any],
    ) -> RuntimeState:
        addr = str(client.get("address") or "")
        if not addr:
            raise BackendError("Cliente de Hyprland sin dirección 'address'.")

        active_ws = _hypr_json("activeworkspace")
        ws_id = int(active_ws["id"]) if isinstance(active_ws, dict) and "id" in active_ws else None

        monitor = self.get_active_monitor(profile)
        geom = compute_geometry(profile, monitor)

        if ws_id is not None:
            _hypr_ipc(f"dispatch movetoworkspacesilent {ws_id},address:{addr}")

        if not client.get("floating", False):
            _hypr_ipc(f"dispatch setfloating address:{addr}")

        if int(client.get("fullscreen") or 0) != 0 or int(client.get("fullscreenClient") or 0) != 0:
            _hypr_ipc(f"dispatch focuswindow address:{addr}")
            _hypr_ipc("dispatch fullscreen 0")

        _hypr_ipc(f"dispatch resizewindowpixel exact {geom.width} {geom.height},address:{addr}")
        _hypr_ipc(f"dispatch movewindowpixel exact {geom.x} {geom.y},address:{addr}")

        if profile.keep_above and not client.get("pinned", False):
            _hypr_ipc(f"dispatch pin address:{addr}")

        opacity = max(0.1, min(1.0, float(profile.opacity)))
        radius = max(0, int(profile.corner_radius))
        _hypr_ipc(f"dispatch setprop address:{addr} noborder 1 lock")
        _hypr_ipc(f"dispatch setprop address:{addr} alpha {opacity:.2f} lock")
        _hypr_ipc(f"dispatch setprop address:{addr} rounding {radius} lock")
        _hypr_ipc(f"dispatch focuswindow address:{addr}")

        raw_pid = client.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else None
        return RuntimeState(
            profile_id=profile.id,
            pid=pid,
            window_id=addr,
            visible=True,
        )

    def _hide_matched_client(
        self,
        profile: DropdownProfile,
        client: dict[str, Any],
        state: RuntimeState,
    ) -> RuntimeState:
        addr = str(client.get("address") or state.window_id)
        raw_pid = client.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else state.pid

        if profile.mode is LifecycleMode.PERSISTENT:
            _hypr_ipc(f"dispatch movetoworkspacesilent {self._special_workspace_name(profile)},address:{addr}")
            return RuntimeState(
                profile_id=profile.id,
                pid=pid,
                window_id=addr,
                visible=False,
            )

        _hypr_ipc(f"dispatch closewindow address:{addr}")
        return RuntimeState(
            profile_id=profile.id,
            pid=None,
            window_id="",
            visible=False,
        )

    def toggle(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        with profile_run_guard(
            self.paths,
            profile.id,
            debounce_ms=self.debounce_ms,
            non_blocking=True,
        ) as acquired:
            if not acquired:
                return state
            return self._toggle_locked(profile, state)

    def _toggle_locked(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        raw_clients = _hypr_json("clients")
        clients = raw_clients if isinstance(raw_clients, list) else []
        live_pids = self._collect_live_pids(profile, state)

        matched = self._find_matching_client(clients, profile, state, live_pids)
        if matched is not None:
            if self._is_client_visible(matched, profile):
                if getattr(profile, "relocate_on_toggle", True) and profile.monitor_policy in (
                    MonitorPolicy.CURSOR,
                    MonitorPolicy.FOCUSED,
                ):
                    target_mon = self.get_active_monitor(profile)
                    if _should_relocate_client(profile, matched, target_mon):
                        return self._apply_show_sequence(profile, matched)
                return self._hide_matched_client(profile, matched, state)
            return self._apply_show_sequence(profile, matched)

        # Cold start (or tray wake)
        launched_pid: int | None = None
        if profile.mode is LifecycleMode.TRAY:
            sni_pid = sni_try_activate_tray_app(profile_match_tokens(profile))
            if sni_pid is not None:
                launched_pid = sni_pid if sni_pid > 0 else None
            else:
                launched_pid = self._launch_app(profile)
        else:
            launched_pid = self._launch_app(profile)

        poll_pids = list(live_pids)
        if launched_pid and launched_pid not in poll_pids:
            poll_pids.append(launched_pid)

        timeout_s = min(int(profile.startup_timeout_ms), 5000) / 1000.0
        deadline = time.monotonic() + timeout_s
        while True:
            raw_poll = _hypr_json("clients")
            poll_clients = raw_poll if isinstance(raw_poll, list) else []
            found = self._find_matching_client(poll_clients, profile, state, poll_pids)
            if found is not None:
                new_state = self._apply_show_sequence(profile, found)
                if new_state.pid is None and launched_pid is not None:
                    new_state.pid = launched_pid
                return new_state
            if time.monotonic() >= deadline:
                break
            time.sleep(0.015)

        return RuntimeState(
            profile_id=profile.id,
            pid=launched_pid,
            window_id="",
            visible=True,
        )

    def hide_or_close(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        raw_clients = _hypr_json("clients")
        clients = raw_clients if isinstance(raw_clients, list) else []
        live_pids = self._collect_live_pids(profile, state)

        matched = self._find_matching_client(clients, profile, state, live_pids)
        if matched is not None:
            return self._hide_matched_client(profile, matched, state)

        if profile.mode is LifecycleMode.EPHEMERAL and live_pids:
            self._terminate_pids(live_pids)
            return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)

        if profile.mode is LifecycleMode.PERSISTENT:
            return RuntimeState(
                profile_id=profile.id,
                pid=live_pids[0] if live_pids else state.pid,
                window_id=state.window_id,
                visible=False,
            )
        return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)

    def diagnose(self) -> list[DiagnosticItem]:
        """Inspect Hyprland environment signature, socket path, and hyprctl availability."""
        items: list[DiagnosticItem] = []
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
        if sig:
            items.append(("ok", "HYPRLAND_INSTANCE_SIGNATURE", f"Definida ({sig})."))
        else:
            items.append(("error", "HYPRLAND_INSTANCE_SIGNATURE", "Variable de entorno no definida."))

        sock_path = _resolve_hypr_socket()
        if sock_path and sock_path.exists():
            items.append(("ok", "Socket IPC Hyprland", f"Accesible: {sock_path}"))
        else:
            items.append(("warn", "Socket IPC Hyprland", "No encontrado en $XDG_RUNTIME_DIR/hypr ni /tmp/hypr."))

        hyprctl_path = shutil.which("hyprctl")
        if hyprctl_path:
            items.append(("ok", "hyprctl", f"Disponible: {hyprctl_path}"))
        else:
            level = "warn" if (sock_path and sock_path.exists()) else "error"
            items.append((level, "hyprctl", "No encontrado en PATH."))

        return items
