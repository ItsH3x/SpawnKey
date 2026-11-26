"""Sway Wayland window backend driven by pure-stdlib i3-ipc socket with swaymsg fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
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


_I3_IPC_MAGIC = b"i3-ipc"
_I3_HEADER_STRUCT = struct.Struct("<II")

IPC_RUN_COMMAND = 0
IPC_GET_WORKSPACES = 1
IPC_GET_OUTPUTS = 3
IPC_GET_TREE = 4


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise OSError("Conexión i3-ipc cerrada inesperadamente.")
        buf.extend(chunk)
    return bytes(buf)


def _swaymsg_cli(msg_type: int, payload: str = "", *, timeout: float = 2.0) -> Any:
    """Fallback to `swaymsg` CLI when $SWAYSOCK is unavailable."""
    swaymsg = shutil.which("swaymsg")
    if not swaymsg:
        raise BackendError("No se encontró 'swaymsg' ni el socket SWAYSOCK.")

    if msg_type == IPC_RUN_COMMAND:
        argv = [swaymsg, "-r", "-t", "command", "--", payload]
    elif msg_type == IPC_GET_WORKSPACES:
        argv = [swaymsg, "-r", "-t", "get_workspaces"]
    elif msg_type == IPC_GET_OUTPUTS:
        argv = [swaymsg, "-r", "-t", "get_outputs"]
    elif msg_type == IPC_GET_TREE:
        argv = [swaymsg, "-r", "-t", "get_tree"]
    else:
        raise BackendError(f"Tipo de mensaje Sway IPC no soportado: {msg_type}")

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError(f"Error ejecutando swaymsg: {exc}") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise BackendError(f"swaymsg falló: {err}")

    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError as exc:
        raise BackendError(f"Salida JSON inválida de swaymsg: {exc}") from exc


def _sway_ipc(msg_type: int, payload: str = "", *, timeout: float = 2.0) -> Any:
    """Send a message over Sway's i3-ipc UNIX socket, falling back to `swaymsg`."""
    swaysock = os.environ.get("SWAYSOCK", "").strip()
    if swaysock and Path(swaysock).exists():
        try:
            payload_bytes = payload.encode("utf-8")
            packet = _I3_IPC_MAGIC + _I3_HEADER_STRUCT.pack(len(payload_bytes), msg_type) + payload_bytes
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(swaysock)
                sock.sendall(packet)
                header = _recv_exact(sock, len(_I3_IPC_MAGIC) + _I3_HEADER_STRUCT.size)
                if header[: len(_I3_IPC_MAGIC)] != _I3_IPC_MAGIC:
                    raise BackendError("Cabecera i3-ipc inválida en respuesta de Sway.")
                resp_len, _resp_type = _I3_HEADER_STRUCT.unpack(header[len(_I3_IPC_MAGIC) :])
                body = _recv_exact(sock, resp_len) if resp_len > 0 else b""
            return json.loads(body.decode("utf-8")) if body else None
        except OSError:
            pass
        except ValueError as exc:
            raise BackendError(f"Respuesta JSON inválida de Sway IPC: {exc}") from exc

    return _swaymsg_cli(msg_type, payload, timeout=timeout)


def _sway_cmd(cmd: str, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Run one or more Sway commands via IPC_RUN_COMMAND."""
    res = _sway_ipc(IPC_RUN_COMMAND, cmd, timeout=timeout)
    if isinstance(res, list):
        for item in res:
            if isinstance(item, dict) and item.get("success") is False:
                err = item.get("error") or cmd
                raise BackendError(f"Comando Sway fallido ({cmd}): {err}")
        return res
    return []


def parse_sway_outputs(outputs: list[dict[str, Any]]) -> tuple[list[MonitorRect], int, int | None]:
    """Parse `GET_OUTPUTS` entries into active MonitorRects, primary_idx, and focused_idx."""
    monitors: list[MonitorRect] = []
    primary_idx = 0
    focused_idx: int | None = None

    for idx, output in enumerate(outputs):
        if not isinstance(output, dict) or not output.get("active", False):
            continue
        rect = output.get("rect")
        if not isinstance(rect, dict):
            continue
        x = int(rect.get("x", 0))
        y = int(rect.get("y", 0))
        w = int(rect.get("width", 0))
        h = int(rect.get("height", 0))
        if w <= 1 or h <= 1:
            continue
        name = str(output.get("name") or f"sway-output-{idx}")
        if output.get("primary"):
            primary_idx = len(monitors)
        if output.get("focused"):
            focused_idx = len(monitors)
        monitors.append(MonitorRect(x=x, y=y, width=w, height=h, name=name))

    return monitors, primary_idx, focused_idx


def _iter_sway_windows(
    node: dict[str, Any],
    *,
    in_scratchpad: bool = False,
) -> list[tuple[dict[str, Any], bool]]:
    """Recursively collect (window_node, in_scratchpad) pairs from Sway GET_TREE."""
    results: list[tuple[dict[str, Any], bool]] = []
    if not isinstance(node, dict):
        return results

    node_name = str(node.get("name") or "")
    node_type = str(node.get("type") or "")
    cur_scratch = in_scratchpad or (node_name == "__i3_scratch")

    is_window = node_type in ("con", "floating_con") and (
        node.get("pid") is not None
        or node.get("app_id") is not None
        or isinstance(node.get("window_properties"), dict)
    )
    if is_window and node.get("id") is not None:
        results.append((node, cur_scratch))

    for child in node.get("nodes") or ():
        if isinstance(child, dict):
            results.extend(_iter_sway_windows(child, in_scratchpad=cur_scratch))
    for child in node.get("floating_nodes") or ():
        if isinstance(child, dict):
            results.extend(_iter_sway_windows(child, in_scratchpad=cur_scratch))

    return results


def _should_relocate_node(
    profile: DropdownProfile,
    node: dict[str, Any],
    target_monitor: MonitorRect,
) -> bool:
    """Return True when a visible Sway node should be relocated or refocused."""
    win_rect: tuple[int, int, int, int] | None = None
    rect = node.get("rect")
    if isinstance(rect, dict):
        try:
            w = int(rect.get("width", 0))
            h = int(rect.get("height", 0))
            if w > 1 and h > 1:
                win_rect = (int(rect.get("x", 0)), int(rect.get("y", 0)), w, h)
        except (TypeError, ValueError):
            win_rect = None

    is_focused: bool | None = None
    if "focused" in node and node["focused"] is not None:
        is_focused = bool(node["focused"])

    return should_relocate_dropdown(
        profile,
        target_monitor,
        win_rect=win_rect,
        is_focused=is_focused,
    )


class SwayWindowBackend(WindowBackend):
    """WindowBackend for Sway using pure-stdlib i3-ipc."""

    name = "sway"
    capabilities = BackendCapabilities(
        supports_opacity=True,
        supports_corner_radius=False,
        supports_click_outside=False,
        supports_animations=False,
        corner_radius_hint="Sway oficial no incluye redondeo de esquinas nativo",
    )

    def __init__(self, paths: AppPaths | None = None, *, debounce_ms: int = 0) -> None:
        self.paths = paths or AppPaths.from_env()
        self.debounce_ms = max(0, int(debounce_ms))

    def get_active_monitor(self, profile: DropdownProfile) -> MonitorRect:
        """Return the active monitor rectangle honoring profile.monitor_policy."""
        try:
            raw_outputs = _sway_ipc(IPC_GET_OUTPUTS)
            if isinstance(raw_outputs, list) and raw_outputs:
                monitors, primary_idx, focused_idx = parse_sway_outputs(raw_outputs)
                if monitors:
                    return select_monitor_by_policy(
                        monitors,
                        profile.monitor_policy,
                        primary_index=primary_idx,
                        focused_index=focused_idx,
                    )
        except Exception:
            pass
        return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    def _collect_live_pids(self, profile: DropdownProfile, state: RuntimeState) -> list[int]:
        pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in pids and is_pid_alive_for_profile(state.pid, profile):
            pids.append(state.pid)
        return pids

    @staticmethod
    def _find_matching_window(
        tree: dict[str, Any],
        profile: DropdownProfile,
        state: RuntimeState,
        live_pids: list[int],
    ) -> tuple[dict[str, Any], bool] | None:
        windows = _iter_sway_windows(tree)

        if state.window_id:
            target_id = state.window_id.strip()
            for node, in_scratch in windows:
                if str(node.get("id")) == target_id:
                    return node, in_scratch

        if live_pids:
            pid_set = set(live_pids)
            for node, in_scratch in windows:
                try:
                    npid = int(node.get("pid") or 0)
                except (TypeError, ValueError):
                    npid = 0
                if npid in pid_set:
                    return node, in_scratch

        tokens = profile_match_tokens(profile)
        if tokens:
            for node, in_scratch in windows:
                wp = node.get("window_properties")
                wp_dict = wp if isinstance(wp, dict) else {}
                fields = (
                    str(node.get("app_id") or ""),
                    str(wp_dict.get("class") or ""),
                    str(wp_dict.get("instance") or ""),
                )
                if any(token_matches(field, tok) for field in fields if field for tok in tokens):
                    return node, in_scratch

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
        node: dict[str, Any],
        *,
        in_scratchpad: bool,
    ) -> RuntimeState:
        con_id = str(node["id"])
        monitor = self.get_active_monitor(profile)
        geom = compute_geometry(profile, monitor)
        opacity = max(0.1, min(1.0, float(profile.opacity)))

        if in_scratchpad:
            _sway_cmd(f"[con_id={con_id}] scratchpad show")
        elif not node.get("visible", True):
            _sway_cmd(f"[con_id={con_id}] move container to workspace current")

        _sway_cmd(
            f"[con_id={con_id}] floating enable, border none, sticky enable, "
            f"resize set {geom.width} px {geom.height} px, "
            f"move position {geom.x} px {geom.y} px, "
            f"opacity set {opacity:.2f}, focus"
        )

        raw_pid = node.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else None
        return RuntimeState(
            profile_id=profile.id,
            pid=pid,
            window_id=con_id,
            visible=True,
        )

    def _hide_matched_window(
        self,
        profile: DropdownProfile,
        node: dict[str, Any],
        state: RuntimeState,
    ) -> RuntimeState:
        con_id = str(node["id"])
        raw_pid = node.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else state.pid

        if profile.mode is LifecycleMode.PERSISTENT:
            _sway_cmd(f"[con_id={con_id}] move scratchpad")
            return RuntimeState(
                profile_id=profile.id,
                pid=pid,
                window_id=con_id,
                visible=False,
            )

        _sway_cmd(f"[con_id={con_id}] kill")
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
        tree = _sway_ipc(IPC_GET_TREE)
        tree_dict = tree if isinstance(tree, dict) else {}
        live_pids = self._collect_live_pids(profile, state)

        matched = self._find_matching_window(tree_dict, profile, state, live_pids)
        if matched is not None:
            node, in_scratchpad = matched
            if not in_scratchpad and node.get("visible", True):
                if getattr(profile, "relocate_on_toggle", True) and profile.monitor_policy in (
                    MonitorPolicy.CURSOR,
                    MonitorPolicy.FOCUSED,
                ):
                    target_mon = self.get_active_monitor(profile)
                    if _should_relocate_node(profile, node, target_mon):
                        return self._apply_show_sequence(profile, node, in_scratchpad=False)
                return self._hide_matched_window(profile, node, state)
            return self._apply_show_sequence(profile, node, in_scratchpad=in_scratchpad)

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
            poll_tree = _sway_ipc(IPC_GET_TREE)
            poll_dict = poll_tree if isinstance(poll_tree, dict) else {}
            found = self._find_matching_window(poll_dict, profile, state, poll_pids)
            if found is not None:
                node, in_scratchpad = found
                new_state = self._apply_show_sequence(profile, node, in_scratchpad=in_scratchpad)
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
        tree = _sway_ipc(IPC_GET_TREE)
        tree_dict = tree if isinstance(tree, dict) else {}
        live_pids = self._collect_live_pids(profile, state)

        matched = self._find_matching_window(tree_dict, profile, state, live_pids)
        if matched is not None:
            node, _ = matched
            return self._hide_matched_window(profile, node, state)

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
        """Inspect SWAYSOCK socket existence and swaymsg CLI availability."""
        items: list[DiagnosticItem] = []
        swaysock = os.environ.get("SWAYSOCK", "").strip()
        sock_exists = bool(swaysock and Path(swaysock).exists())
        if sock_exists:
            items.append(("ok", "SWAYSOCK", f"Socket accesible: {swaysock}"))
        elif swaysock:
            items.append(("warn", "SWAYSOCK", f"Definida pero el socket no existe: {swaysock}"))
        else:
            items.append(("warn", "SWAYSOCK", "Variable de entorno SWAYSOCK no definida."))

        swaymsg_path = shutil.which("swaymsg")
        if swaymsg_path:
            items.append(("ok", "swaymsg", f"Disponible: {swaymsg_path}"))
        else:
            level = "warn" if sock_exists else "error"
            items.append((level, "swaymsg", "No encontrado en PATH."))

        return items
