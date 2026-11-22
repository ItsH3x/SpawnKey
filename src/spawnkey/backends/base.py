"""Abstract interfaces shared by all compositor and shortcut backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shlex

from spawnkey.core.models import DropdownProfile, MonitorRect, RuntimeState


ENV_MARKER = "SPAWNKEY_PROFILE_ID"


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
