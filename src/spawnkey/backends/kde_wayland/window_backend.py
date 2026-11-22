"""KDE Plasma 6 Wayland window backend driven by KWin 6 declarative QML scripts."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Any, Iterator
import uuid

from spawnkey.backends.base import BackendCapabilities, WindowBackend
from spawnkey.backends.kde_wayland.dbus_utils import (
    kwin_is_effect_loaded,
    kwin_is_script_loaded,
    kwin_load_and_run_declarative,
    kwin_load_effect,
    kwin_load_js,
    kwin_reconfigure_effect,
    kwin_stop_and_unload,
    kwin_unload_script,
    sni_try_activate_tray_app,
)
from spawnkey.backends.kde_wayland.qml_templates import (
    build_kwin_config,
    render_existing_window_probe_js,
    render_kwin_qml,
    render_tray_probe_and_dismiss_js,
)
from spawnkey.core.geometry import select_monitor_by_policy
from spawnkey.core.models import DropdownProfile, LifecycleMode, MonitorRect, RuntimeState
from spawnkey.core.storage import AppPaths, profile_run_guard


ENV_MARKER = "SPAWNKEY_PROFILE_ID"
CORNER_EFFECT_NAME = "spawnkey_corners"
PENDING_WATCH_CANCEL_WINDOW_S = 2.5
SNI_WATCH_CANCEL_WINDOW_S = 0.35


def find_profile_pids(profile_id: str, proc_root: Path = Path("/proc")) -> list[int]:
    """Return live process IDs started by SpawnKey for the given profile_id."""
    expected = f"{ENV_MARKER}={profile_id}".encode("utf-8")
    pids: list[int] = []
    if not proc_root.is_dir():
        return pids

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        env_path = entry / "environ"
        try:
            raw = env_path.read_bytes()
        except OSError:
            continue
        if expected in raw.split(b"\x00"):
            # Ignore zombie processes
            try:
                stat_text = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
                rparen = stat_text.rfind(")")
                if rparen != -1 and stat_text[rparen + 2 : rparen + 3] == "Z":
                    continue
            except OSError:
                continue
            pids.append(pid)

    return sorted(pids)


def build_launch_argv(profile: DropdownProfile) -> list[str]:
    """Split profile exec_cmd and optional extra_args into argv tokens."""
    cmd = profile.exec_cmd.strip()
    if profile.extra_args.strip():
        cmd = f"{cmd} {profile.extra_args.strip()}"
    argv = shlex.split(cmd)
    if not argv:
        raise ValueError(f"Comando vacío para el perfil '{profile.id}'.")
    argv[0] = os.path.expanduser(argv[0])
    return argv


def parse_kscreen_outputs(raw_output: str) -> tuple[list[MonitorRect], int]:
    """Parse `kscreen-doctor -o` output into enabled MonitorRects and the primary monitor index."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw_output or "")
    blocks = re.split(r"(?=^Output:\s*\d+)", clean, flags=re.MULTILINE)
    monitors: list[MonitorRect] = []
    primary_idx = 0

    for block in blocks:
        text = block.strip()
        if not text:
            continue
        if re.search(r"\bdisabled\b", text) and not re.search(r"\benabled\b", text):
            continue
        geom_match = re.search(r"Geometry:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", text)
        if not geom_match:
            continue
        x, y, w, h = (int(g) for g in geom_match.groups())
        if w <= 1 or h <= 1:
            continue
        out_match = re.search(r"^Output:\s*\d+\s+(\S+)", text, flags=re.MULTILINE)
        name = out_match.group(1) if out_match else f"kde-output-{len(monitors)}"
        if re.search(r"\b(?:priority\s+1|primary)\b", text):
            primary_idx = len(monitors)
        monitors.append(MonitorRect(x=x, y=y, width=w, height=h, name=name))

    return monitors, primary_idx


class _CapabilitiesDescriptor:
    """Returns default capabilities on class access and dynamic capabilities on instance access."""

    def __init__(self, default: BackendCapabilities) -> None:
        self.default = default

    def __get__(
        self,
        instance: "KDEWaylandWindowBackend | None",
        owner: type | None = None,
    ) -> BackendCapabilities:
        if instance is None:
            return self.default
        return instance._get_capabilities()


class KDEWaylandWindowBackend(WindowBackend):
    """Controls dropdown windows on KDE Plasma 6 Wayland via KWin 6 scripting."""

    name = "kde-wayland"
    capabilities = _CapabilitiesDescriptor(  # type: ignore[assignment]
        BackendCapabilities(
            supports_opacity=True,
            supports_corner_radius=False,
            supports_click_outside=True,
            supports_animations=True,
        )
    )

    def __init__(self, paths: AppPaths | None = None, *, debounce_ms: int = 0) -> None:
        self.paths = paths or AppPaths.from_env()
        self.debounce_ms = max(0, int(debounce_ms))
        self._caps_cache: BackendCapabilities | None = None

    def _get_capabilities(self) -> BackendCapabilities:
        if self._caps_cache is None:
            self._caps_cache = BackendCapabilities(
                supports_opacity=True,
                supports_corner_radius=self._detect_corner_radius_support(),
                supports_click_outside=True,
                supports_animations=True,
            )
        return self._caps_cache

    def refresh_capabilities(self) -> BackendCapabilities:
        """Re-probe KWin Effects D-Bus for optional corner-radius effect support."""
        self._caps_cache = None
        return self._get_capabilities()

    @staticmethod
    def _candidate_effect_plugin_paths() -> list[Path]:
        home = Path.home()
        return [
            home / ".local/lib/qt6/plugins/kwin/effects/plugins/spawnkey_corners.so",
            Path("/usr/lib/qt6/plugins/kwin/effects/plugins/spawnkey_corners.so"),
            Path("/usr/lib64/qt6/plugins/kwin/effects/plugins/spawnkey_corners.so"),
        ]

    def _bootstrap_user_effect_plugin_path(self, plugin_so: Path) -> None:
        """Register ~/.local/lib/qt6/plugins inside a running kwin_wayland session via QML."""
        boot_dir = self.paths.state_dir / "kwin_boot"
        mod_dir = boot_dir / "bootmod"
        try:
            mod_dir.mkdir(parents=True, exist_ok=True)
            link_so = mod_dir / "libspawnkey_corners.so"
            if link_so.exists() or link_so.is_symlink():
                link_so.unlink()
            link_so.symlink_to(plugin_so)
            (mod_dir / "qmldir").write_text(
                "module bootmod\nplugin spawnkey_corners\n",
                encoding="utf-8",
            )
            qml_path = boot_dir / "Main.qml"
            qml_path.write_text(
                'import QtQuick\nimport "bootmod"\nItem {}\n',
                encoding="utf-8",
            )
            sid = kwin_load_and_run_declarative(qml_path, "spawnkey_boot_path", unload_first=True)
            kwin_stop_and_unload("spawnkey_boot_path", sid)
        except Exception:
            pass

    def _detect_corner_radius_support(self) -> bool:
        """Check if spawnkey_corners KWin effect is loaded (or loadable if installed)."""
        try:
            if kwin_is_effect_loaded(CORNER_EFFECT_NAME):
                return True
            installed_so = next((p for p in self._candidate_effect_plugin_paths() if p.is_file()), None)
            if installed_so is None:
                return False
            if kwin_load_effect(CORNER_EFFECT_NAME) and kwin_is_effect_loaded(CORNER_EFFECT_NAME):
                return True
            if str(installed_so).startswith(str(Path.home())):
                self._bootstrap_user_effect_plugin_path(installed_so)
                if kwin_load_effect(CORNER_EFFECT_NAME):
                    return kwin_is_effect_loaded(CORNER_EFFECT_NAME)
        except Exception:
            pass
        return False

    def _corner_radius_config_path(self) -> Path:
        return self.paths.state_dir / "corner_radius.json"

    def _sync_corner_radius_rule(
        self,
        profile: DropdownProfile,
        *,
        active: bool,
        pids: list[int] | None = None,
        window_id: str = "",
    ) -> None:
        """Write or remove per-profile corner_radius rule in $XDG_RUNTIME_DIR/spawnkey/corner_radius.json."""
        cfg_path = self._corner_radius_config_path()
        data: dict = {"profiles": {}}
        if cfg_path.exists():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("profiles"), dict):
                    data = loaded
            except (OSError, ValueError, TypeError):
                pass

        profiles_map: dict = data.setdefault("profiles", {})
        radius = max(0, int(profile.corner_radius))
        changed = False

        if active and radius > 0:
            entry = {
                "corner_radius": radius,
                "mode": profile.mode.value,
                "pids": [int(p) for p in (pids or []) if p and int(p) > 0],
                "match_hint": (profile.match_hint or "").strip().lower(),
                "desktop_id": (profile.desktop_id or "").strip().lower(),
                "window_id": window_id or "",
            }
            if profiles_map.get(profile.id) != entry:
                profiles_map[profile.id] = entry
                changed = True
        else:
            if profile.id in profiles_map:
                profiles_map.pop(profile.id, None)
                changed = True

        if changed or not cfg_path.exists():
            try:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = cfg_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp_path, cfg_path)
            except OSError:
                return
        elif not (active and radius > 0):
            return

        if self._caps_cache is not None and self._caps_cache.supports_corner_radius:
            kwin_reconfigure_effect(CORNER_EFFECT_NAME)
        elif radius > 0 and self._detect_corner_radius_support():
            self._caps_cache = BackendCapabilities(
                supports_opacity=True,
                supports_corner_radius=True,
                supports_click_outside=True,
                supports_animations=True,
            )
            kwin_reconfigure_effect(CORNER_EFFECT_NAME)

    def _legacy_plugin_name(self, profile_id: str) -> str:
        return f"spawnkey_win_{profile_id}"

    def _new_plugin_name(self, profile_id: str) -> str:
        return f"spawnkey_win_{profile_id}_{uuid.uuid4().hex[:8]}"

    def _kwin_handle_path(self, profile_id: str) -> Path:
        return self.paths.state_dir / "kwin" / f"{profile_id}.json"

    def _load_kwin_handle(self, profile_id: str) -> tuple[str | None, int | None]:
        path = self._kwin_handle_path(profile_id)
        if not path.exists():
            return None, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            plugin_name = data.get("plugin_name")
            script_id = data.get("script_id")
            return (
                str(plugin_name) if plugin_name else None,
                int(script_id) if isinstance(script_id, int) else None,
            )
        except (OSError, ValueError, TypeError):
            return None, None

    def _load_kwin_handle_meta(self, profile_id: str) -> tuple[str, float | None, bool]:
        path = self._kwin_handle_path(profile_id)
        if not path.exists():
            return "", None, False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            action = str(data.get("action") or "")
            raw_ts = data.get("ts")
            ts = float(raw_ts) if isinstance(raw_ts, (int, float)) else None
            sni = bool(data.get("sni", False))
            return action, ts, sni
        except (OSError, ValueError, TypeError):
            return "", None, False

    def _load_kwin_handle_action(self, profile_id: str) -> str:
        action, _, _ = self._load_kwin_handle_meta(profile_id)
        return action

    def _is_recent_watch_new(
        self,
        profile_id: str,
        *,
        max_age_s: float | None = None,
    ) -> bool:
        """Return True only when the active KWin handle is a recent watch_new."""
        action, ts, sni = self._load_kwin_handle_meta(profile_id)
        if action != "watch_new" or ts is None:
            return False
        limit = (
            max_age_s
            if max_age_s is not None
            else (SNI_WATCH_CANCEL_WINDOW_S if sni else PENDING_WATCH_CANCEL_WINDOW_S)
        )
        age = time.monotonic() - ts
        return 0.0 <= age < limit

    def _save_kwin_handle(
        self,
        profile_id: str,
        plugin_name: str,
        script_id: int,
        action: str = "",
        *,
        ts: float | None = None,
        sni: bool = False,
    ) -> None:
        path = self._kwin_handle_path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "plugin_name": plugin_name,
                    "script_id": script_id,
                    "action": action,
                    "ts": time.monotonic() if ts is None else float(ts),
                    "sni": bool(sni),
                }
            ),
            encoding="utf-8",
        )

    def _mark_kwin_handle_action(self, profile_id: str, action: str, *, sni: bool | None = None) -> None:
        """Update action/sni flag on an existing KWin handle file without unloading the script."""
        path = self._kwin_handle_path(profile_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            data["action"] = action
            if sni is not None:
                data["sni"] = bool(sni)
            path.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError, TypeError):
            pass

    @contextmanager
    def _profile_lock(
        self,
        profile_id: str,
        *,
        non_blocking: bool = True,
        debounce_ms: int = 0,
    ) -> Iterator[bool]:
        """Acquire per-profile lock without a daemon; drops overlapping calls when non_blocking."""
        with profile_run_guard(
            self.paths,
            profile_id,
            debounce_ms=debounce_ms,
            non_blocking=non_blocking,
        ) as acquired:
            yield acquired

    def _clear_kwin_handle(self, profile_id: str) -> None:
        path = self._kwin_handle_path(profile_id)
        try:
            path.unlink()
        except OSError:
            pass

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
                data["noBorder"] = False
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
        clean_payload["noBorder"] = False
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(clean_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    def _ensure_orig_state(self, profile: DropdownProfile) -> dict[str, Any]:
        existing = self.load_orig_state(profile.id)
        if existing is not None:
            return existing
        baseline: dict[str, Any] = {
            "profile_id": profile.id,
            "x": 0,
            "y": 0,
            "width": 0,
            "height": 0,
            "noBorder": False,
            "keepAbove": False,
            "keepBelow": False,
            "skipTaskbar": False,
            "skipPager": False,
            "skipSwitcher": False,
            "onAllDesktops": False,
            "maximizeMode": 0,
            "fullScreen": False,
            "skipsCloseAnimation": False,
            "opacity": 1.0,
        }
        self.save_orig_state(profile.id, baseline)
        return baseline

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

    def _has_active_watcher(self, profile_id: str) -> bool:
        plugin_name, _ = self._load_kwin_handle(profile_id)
        if plugin_name:
            return kwin_is_script_loaded(plugin_name)
        return kwin_is_script_loaded(self._legacy_plugin_name(profile_id))

    def _stop_previous_kwin_script(self, profile_id: str) -> None:
        plugin_name, script_id = self._load_kwin_handle(profile_id)
        if plugin_name:
            kwin_stop_and_unload(plugin_name, script_id)
        else:
            kwin_unload_script(self._legacy_plugin_name(profile_id))
        self._clear_kwin_handle(profile_id)

    def _run_profile_qml(self, profile: DropdownProfile, cfg: dict) -> tuple[str, int]:
        """Stop any previous KWin script for this profile and run a fresh declarative QML script."""
        self._stop_previous_kwin_script(profile.id)
        plugin_name = self._new_plugin_name(profile.id)
        cfg["pluginName"] = plugin_name
        qml_path = self._write_unique_qml(profile.id, render_kwin_qml(cfg))
        script_id = kwin_load_and_run_declarative(qml_path, plugin_name, unload_first=False)
        self._save_kwin_handle(profile.id, plugin_name, script_id, action=str(cfg.get("action", "")))
        return plugin_name, script_id

    def _write_unique_js(self, prefix_tag: str, content: str) -> Path:
        js_root = self.paths.state_dir / "scripts_js"
        js_root.mkdir(parents=True, exist_ok=True)
        for old in js_root.glob(f"{prefix_tag}_*.js"):
            try:
                old.unlink()
            except OSError:
                pass
        nonce = uuid.uuid4().hex[:10]
        js_path = js_root / f"{prefix_tag}_{nonce}.js"
        js_path.write_text(content, encoding="utf-8")
        return js_path

    def _wait_for_probe_done(self, done_flag: str, *, max_wait_s: float = 0.06, step_s: float = 0.003) -> bool:
        """Poll KWin until the probe script unloads its completion marker (typically ~2-5ms)."""
        deadline = time.monotonic() + max_wait_s
        while True:
            if not kwin_is_script_loaded(done_flag):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step_s)

    def _probe_tray_window(
        self,
        profile: DropdownProfile,
        *,
        allow_dismiss: bool,
        saved_original: dict[str, Any] | None = None,
        has_active_watcher: bool = True,
    ) -> str:
        """Probe KWin for a tray app's current window state.

        Returns:
            "dismissed": An open SpawnKey dropdown was found and nudged to restore+close.
            "normal_open": The app already has a normal visible window (e.g. on another monitor).
            "in_tray": No visible window found (app is in tray or not running).
        """
        nonce = uuid.uuid4().hex[:8]
        sig_name = f"spawnkey_sig_{profile.id}_{nonce}"
        dismiss_flag = f"spawnkey_flag_{profile.id}_{nonce}"
        normal_flag = f"spawnkey_norm_{profile.id}_{nonce}"
        done_flag = f"spawnkey_done_{profile.id}_{nonce}"

        flag_js = self._write_unique_js(f"flag_{profile.id}", "// spawnkey live probe marker\n")
        cfg = build_kwin_config(
            profile,
            action="hide_existing",
            candidate_pids=[],
            plugin_name=sig_name,
            saved_original=saved_original,
            has_active_watcher=has_active_watcher,
        )
        probe_js = self._write_unique_js(
            f"probe_{profile.id}",
            render_tray_probe_and_dismiss_js(
                cfg,
                dismiss_flag if allow_dismiss else "",
                normal_flag,
                done_flag,
            ),
        )

        dismiss_sid: int | None = None
        normal_sid: int | None = None
        done_sid: int | None = None
        probe_sid: int | None = None
        dismiss_unloaded = False
        normal_unloaded = False
        done_unloaded = False
        try:
            if allow_dismiss:
                dismiss_sid = kwin_load_js(flag_js, dismiss_flag, run=False, unload_first=False)
            normal_sid = kwin_load_js(flag_js, normal_flag, run=False, unload_first=False)
            done_sid = kwin_load_js(flag_js, done_flag, run=False, unload_first=False)
            probe_sid = kwin_load_js(probe_js, sig_name, run=True, unload_first=False)

            done_unloaded = self._wait_for_probe_done(done_flag)
            if allow_dismiss and not kwin_is_script_loaded(dismiss_flag):
                dismiss_unloaded = True
                return "dismissed"
            if not kwin_is_script_loaded(normal_flag):
                normal_unloaded = True
                return "normal_open"
            return "in_tray"
        finally:
            kwin_stop_and_unload(sig_name, probe_sid)
            if allow_dismiss and not dismiss_unloaded:
                kwin_stop_and_unload(dismiss_flag, dismiss_sid)
            if not normal_unloaded:
                kwin_stop_and_unload(normal_flag, normal_sid)
            if not done_unloaded:
                kwin_stop_and_unload(done_flag, done_sid)

    def _probe_existing_window(self, profile: DropdownProfile, state: RuntimeState) -> bool:
        """Check if a matching window exists in KWin for single-instance persistent/ephemeral apps."""
        if not (profile.match_hint or profile.desktop_id or state.window_id):
            return False

        nonce = uuid.uuid4().hex[:8]
        sig_name = f"spawnkey_exsig_{profile.id}_{nonce}"
        found_flag = f"spawnkey_exfnd_{profile.id}_{nonce}"
        done_flag = f"spawnkey_exdone_{profile.id}_{nonce}"

        flag_js = self._write_unique_js(f"flag_{profile.id}", "// spawnkey live probe marker\n")
        allow_fallback = bool(state.window_id or state.pid is not None or state.visible)
        cfg = build_kwin_config(
            profile,
            action="toggle_existing",
            candidate_pids=[],
            plugin_name=sig_name,
            expected_window_id=state.window_id,
            allow_hint_fallback=allow_fallback,
        )
        probe_js = self._write_unique_js(
            f"exprobe_{profile.id}",
            render_existing_window_probe_js(cfg, found_flag, done_flag),
        )

        found_sid: int | None = None
        done_sid: int | None = None
        probe_sid: int | None = None
        found_unloaded = False
        done_unloaded = False
        try:
            found_sid = kwin_load_js(flag_js, found_flag, run=False, unload_first=False)
            done_sid = kwin_load_js(flag_js, done_flag, run=False, unload_first=False)
            probe_sid = kwin_load_js(probe_js, sig_name, run=True, unload_first=False)
            done_unloaded = self._wait_for_probe_done(done_flag)
            if not kwin_is_script_loaded(found_flag):
                found_unloaded = True
                return True
            return False
        finally:
            kwin_stop_and_unload(sig_name, probe_sid)
            if not found_unloaded:
                kwin_stop_and_unload(found_flag, found_sid)
            if not done_unloaded:
                kwin_stop_and_unload(done_flag, done_sid)

    def _write_unique_qml(self, profile_id: str, content: str) -> Path:
        qml_root = self.paths.state_dir / "scripts_qml"
        qml_root.mkdir(parents=True, exist_ok=True)
        # Clean older run directories for this profile to prevent buildup
        prefix = f"run_{profile_id}_"
        for old in qml_root.glob(f"{prefix}*"):
            if old.is_dir():
                for child in old.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    old.rmdir()
                except OSError:
                    pass
            else:
                try:
                    old.unlink()
                except OSError:
                    pass

        nonce = uuid.uuid4().hex[:10]
        run_dir = qml_root / f"{prefix}{nonce}"
        run_dir.mkdir(parents=True, exist_ok=True)
        qml_path = run_dir / "Main.qml"
        qml_path.write_text(content, encoding="utf-8")
        return qml_path

    @staticmethod
    def _profile_sni_tokens(profile: DropdownProfile) -> list[str]:
        tokens: list[str] = []
        if profile.match_hint and profile.match_hint.strip():
            tokens.append(profile.match_hint.strip().lower())
        if profile.desktop_id and profile.desktop_id.strip():
            desk = profile.desktop_id.strip().lower()
            if desk.endswith(".desktop"):
                desk = desk[:-8]
            if desk and desk not in tokens:
                tokens.append(desk)
        try:
            argv = build_launch_argv(profile)
            if argv:
                exe_base = Path(argv[0]).name.strip().lower()
                if exe_base and exe_base not in tokens:
                    tokens.append(exe_base)
        except ValueError:
            pass
        return tokens

    def _activate_or_launch_tray_app(self, profile: DropdownProfile) -> tuple[int, bool]:
        """Try instant StatusNotifierItem D-Bus activation (~2ms), else cold-launch binary."""
        sni_pid = sni_try_activate_tray_app(self._profile_sni_tokens(profile))
        if sni_pid is not None:
            return (sni_pid if sni_pid > 0 else 0), True
        return self._launch_app(profile), False

    def _launch_app(self, profile: DropdownProfile) -> int:
        argv = build_launch_argv(profile)
        env = dict(os.environ)
        env[ENV_MARKER] = profile.id
        # Strip GTK CSD where supported if user launches a GTK app as dropdown
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

    def get_active_monitor(self, profile: DropdownProfile) -> MonitorRect:
        """Query monitor geometry honoring profile.monitor_policy."""
        try:
            proc = subprocess.run(
                ["kscreen-doctor", "-o"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0:
                monitors, primary_idx = parse_kscreen_outputs(proc.stdout)
                if monitors:
                    return select_monitor_by_policy(
                        monitors,
                        profile.monitor_policy,
                        primary_index=primary_idx,
                    )
        except (OSError, subprocess.SubprocessError):
            pass
        return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    def toggle(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        with self._profile_lock(
            profile.id,
            non_blocking=True,
            debounce_ms=self.debounce_ms,
        ) as acquired:
            if not acquired:
                return state
            return self._toggle_locked(profile, state)

    def _toggle_locked(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        if profile.mode is LifecycleMode.TRAY:
            has_watcher = self._has_active_watcher(profile.id)
            orig_state = self.load_orig_state(profile.id)
            allow_dismiss = has_watcher or (orig_state is not None)
            probe_state = self._probe_tray_window(
                profile,
                allow_dismiss=allow_dismiss,
                saved_original=orig_state,
                has_active_watcher=has_watcher,
            )
            if probe_state == "dismissed":
                # Keep corner_radius rule alive during exit animation; spawnkey_corners clears
                # radius automatically when noBorder is restored or windowClosed fires.
                self.clear_orig_state(profile.id)
                if not has_watcher:
                    self._clear_kwin_handle(profile.id)
                else:
                    self._mark_kwin_handle_action(profile.id, "dismissed")
                return RuntimeState(profile_id=profile.id, pid=None, visible=False)

            # Second press while a recent watch_new open (< 2.5 s cold start / < 0.35 s SNI)
            # is still waiting for the app to map: cancel cleanly without duplicate launch.
            if (
                probe_state == "in_tray"
                and has_watcher
                and self._is_recent_watch_new(profile.id)
            ):
                cancel_cfg = build_kwin_config(
                    profile,
                    action="cancel_pending",
                    candidate_pids=[],
                    plugin_name="",
                    saved_original=orig_state,
                )
                self._run_profile_qml(profile, cancel_cfg)
                self.clear_orig_state(profile.id)
                return RuntimeState(profile_id=profile.id, pid=None, visible=False)

            orig_state = self._ensure_orig_state(profile)
            self._sync_corner_radius_rule(profile, active=True)
            cfg = build_kwin_config(
                profile,
                action="watch_new",
                candidate_pids=[],
                plugin_name="",
                saved_original=orig_state,
            )
            self._run_profile_qml(profile, cfg)
            if probe_state == "normal_open":
                self._mark_kwin_handle_action(profile.id, "active_dropdown")
                return RuntimeState(profile_id=profile.id, pid=None, visible=True)

            new_pid, via_sni = self._activate_or_launch_tray_app(profile)
            if via_sni:
                self._mark_kwin_handle_action(profile.id, "watch_new", sni=True)
            if new_pid > 0:
                self._sync_corner_radius_rule(profile, active=True, pids=[new_pid])
            return RuntimeState(profile_id=profile.id, pid=new_pid if new_pid > 0 else None, visible=True)

        live_pids = find_profile_pids(profile.id)

        # Also check previously recorded PID in case process forked/cleared env
        if state.pid and state.pid not in live_pids and Path(f"/proc/{state.pid}").exists():
            live_pids.append(state.pid)

        # If user pressed the shortcut again while a newly launched app hasn't mapped its window yet,
        # cancel the pending open cleanly instead of losing track of the arriving window.
        if (
            live_pids
            and state.visible
            and self._has_active_watcher(profile.id)
            and self._is_recent_watch_new(profile.id)
            and not self._probe_existing_window(profile, state)
        ):
            cancel_cfg = build_kwin_config(
                profile,
                action="cancel_pending",
                candidate_pids=live_pids,
                plugin_name="",
                expected_window_id=state.window_id,
            )
            self._run_profile_qml(profile, cancel_cfg)
            if profile.mode is LifecycleMode.EPHEMERAL:
                for pid in live_pids:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)
            return RuntimeState(
                profile_id=profile.id,
                pid=live_pids[0],
                window_id=state.window_id,
                visible=False,
            )

        # Fallback for single-instance apps whose launcher delegated to main process and exited
        fallback_found = False
        if not live_pids:
            fallback_found = self._probe_existing_window(profile, state)

        if live_pids or fallback_found:
            will_be_visible = not state.visible
            if will_be_visible or (profile.mode is LifecycleMode.PERSISTENT and profile.corner_radius > 0):
                self._sync_corner_radius_rule(
                    profile,
                    active=True,
                    pids=live_pids,
                    window_id=state.window_id,
                )

            cfg = build_kwin_config(
                profile,
                action="toggle_existing",
                candidate_pids=live_pids,
                plugin_name="",
                expected_window_id=state.window_id,
                allow_hint_fallback=fallback_found,
            )
            self._run_profile_qml(profile, cfg)

            if profile.mode is LifecycleMode.EPHEMERAL and state.visible:
                return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)
            return RuntimeState(
                profile_id=profile.id,
                pid=live_pids[0] if live_pids else state.pid,
                window_id=state.window_id,
                visible=will_be_visible,
            )

        # No running instance for this profile: arm KWin watcher first, then spawn app.
        self._sync_corner_radius_rule(profile, active=True, window_id=state.window_id)
        cfg = build_kwin_config(
            profile,
            action="watch_new",
            candidate_pids=[],
            plugin_name="",
            expected_window_id=state.window_id,
        )
        self._run_profile_qml(profile, cfg)

        new_pid = self._launch_app(profile)
        self._sync_corner_radius_rule(profile, active=True, pids=[new_pid], window_id=state.window_id)
        return RuntimeState(profile_id=profile.id, pid=new_pid, window_id=state.window_id, visible=True)

    def hide_or_close(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        with self._profile_lock(profile.id, non_blocking=False, debounce_ms=0):
            return self._hide_or_close_locked(profile, state)

    def _hide_or_close_locked(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        self._sync_corner_radius_rule(profile, active=False)
        if profile.mode is LifecycleMode.TRAY:
            has_watcher = self._has_active_watcher(profile.id)
            orig_state = self.load_orig_state(profile.id)
            if has_watcher or orig_state is not None:
                self._probe_tray_window(
                    profile,
                    allow_dismiss=True,
                    saved_original=orig_state,
                    has_active_watcher=has_watcher,
                )
            else:
                self._stop_previous_kwin_script(profile.id)
            self.clear_orig_state(profile.id)
            return RuntimeState(profile_id=profile.id, pid=None, visible=False)

        live_pids = find_profile_pids(profile.id)
        if state.pid and state.pid not in live_pids and Path(f"/proc/{state.pid}").exists():
            live_pids.append(state.pid)

        fallback_found = False
        if not live_pids:
            fallback_found = self._probe_existing_window(profile, state)

        if not live_pids and not fallback_found:
            self._stop_previous_kwin_script(profile.id)
            return RuntimeState(profile_id=profile.id, pid=None, visible=False)

        cfg = build_kwin_config(
            profile,
            action="hide_existing",
            candidate_pids=live_pids,
            plugin_name="",
            expected_window_id=state.window_id,
            allow_hint_fallback=fallback_found,
        )
        self._run_profile_qml(profile, cfg)

        if profile.mode is LifecycleMode.EPHEMERAL:
            return RuntimeState(profile_id=profile.id, pid=None, window_id="", visible=False)
        return RuntimeState(
            profile_id=profile.id,
            pid=live_pids[0] if live_pids else state.pid,
            window_id=state.window_id,
            visible=False,
        )
