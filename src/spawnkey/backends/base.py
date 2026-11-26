"""Abstract interfaces shared by all compositor and shortcut backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shlex

from spawnkey.core.models import (
    DropdownProfile,
    MonitorRect,
    RuntimeState,
    infer_match_hint_from_cmd,
)


ENV_MARKER = "SPAWNKEY_PROFILE_ID"
DiagnosticItem = tuple[str, str, str]
_IGNORED_RDNS_SEGMENTS = frozenset(
    {"desktop", "client", "linux", "github", "com", "org", "net", "io", "app"}
)
_GENERIC_SHELL_BINS = frozenset({"python", "python3", "sh", "bash", "env"})


def token_matches(candidate: str, hint: str) -> bool:
    """Generic case-insensitive token matcher shared across compositor, SNI, and /proc checks."""
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
        for seg in reversed(norm_hint.split(".")):
            if len(seg) < 4 or seg in _IGNORED_RDNS_SEGMENTS:
                continue
            if norm_cand == seg or seg in norm_cand:
                return True
            seg_dash = seg.find("-")
            if seg_dash >= 4:
                seg_stem = seg[:seg_dash]
                if norm_cand == seg_stem or seg_stem in norm_cand:
                    return True
    return False


def profile_match_tokens(profile: DropdownProfile) -> list[str]:
    """Return deduplicated lowercase match tokens for a DropdownProfile."""
    tokens: list[str] = []

    def _add(raw: str | None) -> None:
        if not raw:
            return
        tok = raw.strip().lower()
        if tok and tok not in tokens:
            tokens.append(tok)

    _add(profile.match_hint)
    if profile.desktop_id and profile.desktop_id.strip():
        desk = profile.desktop_id.strip().lower()
        if desk.endswith(".desktop"):
            desk = desk[:-8]
        _add(desk)
    if profile.exec_cmd and profile.exec_cmd.strip():
        _add(infer_match_hint_from_cmd(profile.exec_cmd))
    try:
        argv = build_launch_argv(profile)
        if argv:
            _add(Path(argv[0]).name)
    except ValueError:
        pass
    return tokens


def is_pid_alive_for_profile(
    pid: int | None,
    profile: DropdownProfile,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Return True if pid is alive (non-zombie) and matches profile via env marker or cmdline/comm."""
    if not pid or pid <= 0:
        return False
    proc_dir = proc_root / str(pid)
    if not proc_dir.is_dir():
        return False

    try:
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8", errors="ignore")
        rparen = stat_text.rfind(")")
        if rparen != -1 and stat_text[rparen + 2 : rparen + 3] == "Z":
            return False
    except FileNotFoundError:
        return False
    except OSError:
        pass

    expected = f"{ENV_MARKER}={profile.id}".encode("utf-8")
    try:
        raw_env = (proc_dir / "environ").read_bytes()
        if expected in raw_env.split(b"\x00"):
            return True
    except OSError:
        pass

    hints = profile_match_tokens(profile)
    if not hints:
        return False

    candidates: list[str] = []
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

    return any(token_matches(cand, hint) for cand in candidates for hint in hints)


def prepare_launch_argv_and_env(
    profile: DropdownProfile,
    base_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build launch argv and environment, injecting SPAWNKEY_PROFILE_ID into Flatpak sandboxes."""
    argv = build_launch_argv(profile)
    env = dict(os.environ if base_env is None else base_env)
    env[ENV_MARKER] = profile.id
    env.setdefault("GTK_CSD", "0")

    if Path(argv[0]).name == "flatpak" and "run" in argv[1:]:
        run_idx = argv.index("run", 1)
        marker_prefix = f"--env={ENV_MARKER}="
        if not any(arg.startswith(marker_prefix) for arg in argv):
            argv.insert(run_idx + 1, f"{marker_prefix}{profile.id}")

    return argv, env


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


class BackendError(RuntimeError):
    """Base exception raised when a compositor or shortcut backend operation fails."""


@dataclass(frozen=True)
class BackendCapabilities:
    """Declarative feature capabilities supported by a WindowBackend."""

    supports_opacity: bool = False
    supports_corner_radius: bool = False
    supports_click_outside: bool = False
    supports_animations: bool = False
    corner_radius_hint: str = ""
    backend_hint: str = ""


class ShortcutRegistrationStatus(str, Enum):
    ACTIVE = "active"
    UNASSIGNED = "unassigned"
    DISABLED = "disabled"
    OUT_OF_SYNC = "out_of_sync"


class WindowBackend(ABC):
    """Contract implemented by each display-server/compositor backend."""

    name: str
    capabilities: BackendCapabilities = BackendCapabilities()

    @abstractmethod
    def get_active_monitor(self, profile: DropdownProfile) -> MonitorRect:
        """Return the active monitor rectangle according to the profile policy."""

    @abstractmethod
    def toggle(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        """Toggle visibility or lifecycle of the dropdown without duplicating instances."""

    @abstractmethod
    def hide_or_close(self, profile: DropdownProfile, state: RuntimeState) -> RuntimeState:
        """Hide (persistent mode) or close (ephemeral mode) the dropdown window."""

    def diagnose(self) -> list[DiagnosticItem]:
        """Run backend-specific health checks and return diagnostic items."""
        return []


class ShortcutBackend(ABC):
    """Contract for registering, auditing, and cleanly removing SpawnKey shortcuts."""

    name: str

    @abstractmethod
    def register_shortcut(self, profile: DropdownProfile) -> None:
        """Register or update the global shortcut pointing to profile.script_path."""

    @abstractmethod
    def remove_shortcut(self, profile: DropdownProfile) -> bool:
        """Remove the global shortcut cleanly without touching non-SpawnKey shortcuts."""

    @abstractmethod
    def remove_shortcut_by_component(self, component_id: str) -> bool:
        """Remove a SpawnKey-managed shortcut entry cleanly by its backend component ID."""

    @abstractmethod
    def get_shortcut_status(self, profile: DropdownProfile) -> ShortcutRegistrationStatus:
        """Check whether the registered system shortcut matches the profile."""

    @abstractmethod
    def list_managed_shortcut_ids(self) -> list[str]:
        """List all shortcut IDs currently registered under SpawnKey's namespace."""

    def diagnose(self, profiles: list[DropdownProfile] | None = None) -> list[DiagnosticItem]:
        """Run shortcut-backend health checks and return diagnostic items."""
        return []

    def find_system_conflicts(self, shortcut: str) -> list[str]:
        """Return human-readable descriptions of OS shortcuts colliding with the given shortcut."""
        return []
