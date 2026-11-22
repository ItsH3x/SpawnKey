"""High-level coordinator shared by SpawnKey CLI and GTK GUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from spawnkey.backends.base import (
    BackendCapabilities,
    BackendError,
    ShortcutBackend,
    ShortcutRegistrationStatus,
    WindowBackend,
)
from spawnkey.backends.detector import EnvironmentInfo, detect_environment
from spawnkey.backends.factory import UnsupportedBackendError, get_backends
from spawnkey.core.models import DropdownProfile, MonitorRect
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import find_profile_shortcut_conflicts
from spawnkey.core.storage import AppPaths, ProfileStore, RuntimeStateStore


@dataclass(frozen=True)
class ManagedEntry:
    """Combined view of a dropdown profile and its live system shortcut state."""

    profile: DropdownProfile
    shortcut_status: ShortcutRegistrationStatus
    script_exists: bool


class SpawnKeyManager:
    """Orchestrates profiles, script generation, shortcuts, and live testing."""

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        window_backend: WindowBackend | None = None,
        shortcut_backend: ShortcutBackend | None = None,
        env_info: EnvironmentInfo | None = None,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        self.store = ProfileStore(self.paths)
        self.state_store = RuntimeStateStore(self.paths)
        self.script_gen = ScriptGenerator(self.paths)
        self.env_info = env_info or detect_environment()

        self._window_backend = window_backend
        self._shortcut_backend = shortcut_backend
        self._backend_error: str = ""

        if self._window_backend is None or self._shortcut_backend is None:
            try:
                wb, sb = get_backends(self.paths, env_info=self.env_info)
                self._window_backend = self._window_backend or wb
                self._shortcut_backend = self._shortcut_backend or sb
            except UnsupportedBackendError as exc:
                self._backend_error = str(exc)

    @property
    def backend_available(self) -> bool:
        return self._window_backend is not None and self._shortcut_backend is not None

    @property
    def backend_error(self) -> str:
        return self._backend_error

    @property
    def capabilities(self) -> BackendCapabilities:
        if self._window_backend is not None:
            return self._window_backend.capabilities
        return BackendCapabilities()

    def get_active_monitor(self, profile: DropdownProfile | None = None) -> MonitorRect:
        if self._window_backend is not None:
            try:
                ref = profile or DropdownProfile(id="ref", name="Ref", exec_cmd="true")
                return self._window_backend.get_active_monitor(ref)
            except Exception:
                pass
        return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    def list_entries(self) -> list[ManagedEntry]:
        entries: list[ManagedEntry] = []
        for profile in self.store.list_profiles():
            if self._shortcut_backend is not None:
                status = self._shortcut_backend.get_shortcut_status(profile)
            elif not profile.shortcut:
                status = ShortcutRegistrationStatus.UNASSIGNED
            elif not profile.shortcut_enabled:
                status = ShortcutRegistrationStatus.DISABLED
            else:
                status = ShortcutRegistrationStatus.OUT_OF_SYNC

            script_ok = bool(profile.script_path and Path(profile.script_path).exists())
            entries.append(
                ManagedEntry(
                    profile=profile,
                    shortcut_status=status,
                    script_exists=script_ok,
                )
            )
        return entries

    def list_orphaned_shortcuts(self) -> list[str]:
        if self._shortcut_backend is None:
            return []
        known = {p.shortcut_backend_id for p in self.store.list_profiles()}
        managed = self._shortcut_backend.list_managed_shortcut_ids()
        return sorted(comp for comp in managed if comp not in known)

    def find_shortcut_conflicts(
        self,
        shortcut: str,
        *,
        exclude_profile_id: str | None = None,
    ) -> list[DropdownProfile]:
        """Return other enabled profiles that already use the same canonical shortcut."""
        return find_profile_shortcut_conflicts(
            self.store.list_profiles(),
            shortcut,
            exclude_profile_id=exclude_profile_id,
        )

    def save_profile(
        self,
        profile: DropdownProfile,
        *,
        sync_shortcut: bool = True,
    ) -> tuple[DropdownProfile, str]:
        """Validate, generate .sh script, sync desktop shortcut, and persist JSON."""
        profile.validate()
        self.script_gen.generate(profile)

        shortcut_msg = ""
        if sync_shortcut and self._shortcut_backend is not None:
            try:
                if profile.shortcut and profile.shortcut_enabled:
                    self._shortcut_backend.register_shortcut(profile)
                    shortcut_msg = f"Atajo '{profile.shortcut}' activo."
                else:
                    removed = self._shortcut_backend.remove_shortcut(profile)
                    shortcut_msg = "Atajo removido del sistema." if removed else "Guardado sin atajo."
            except (BackendError, ValueError) as exc:
                profile.shortcut_enabled = False
                self.store.save_profile(profile)
                raise ValueError(f"Perfil guardado, pero falló el atajo: {exc}") from exc

        self.store.save_profile(profile)
        return profile, shortcut_msg

    def remove_shortcut_only(self, profile_id: str, *, keep_keys: bool = False) -> bool:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"Perfil '{profile_id}' no encontrado.")

        removed = False
        if self._shortcut_backend is not None:
            removed = self._shortcut_backend.remove_shortcut(profile)

        profile.shortcut_enabled = False
        if not keep_keys:
            profile.shortcut = ""
        self.store.save_profile(profile)
        return removed

    def apply_shortcut(self, profile_id: str, keys: str | None = None) -> DropdownProfile:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"Perfil '{profile_id}' no encontrado.")
        if keys is not None:
            profile.shortcut = keys
        if not profile.shortcut:
            raise ValueError("Asigna primero una combinación de teclas.")
        if self._shortcut_backend is None:
            raise UnsupportedBackendError(self._backend_error or "Backend de atajos no disponible.")

        self.script_gen.generate(profile)
        self._shortcut_backend.register_shortcut(profile)
        self.store.save_profile(profile)
        return profile

    def delete_profile(self, profile_id: str) -> dict[str, bool]:
        profile = self.store.get_profile(profile_id)
        removed_shortcut = False

        if profile is not None:
            if self._shortcut_backend is not None:
                try:
                    removed_shortcut = self._shortcut_backend.remove_shortcut(profile)
                except BackendError:
                    pass
            if self._window_backend is not None:
                try:
                    state = self.state_store.load_state(profile.id)
                    self._window_backend.hide_or_close(profile, state)
                except BackendError:
                    pass

        deleted_profile = self.store.delete_profile(profile_id)
        deleted_script = self.script_gen.remove(profile_id)
        self.state_store.clear_state(profile_id)
        return {
            "profile": deleted_profile,
            "script": deleted_script,
            "shortcut": removed_shortcut,
        }

    def cleanup_orphaned_shortcuts(self) -> list[str]:
        orphans = self.list_orphaned_shortcuts()
        cleaned: list[str] = []
        if self._shortcut_backend is not None:
            for comp in orphans:
                self._shortcut_backend.remove_shortcut_by_component(comp)
                cleaned.append(comp)
        return cleaned

    def test_profile(self, profile_id: str) -> None:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"Perfil '{profile_id}' no encontrado.")
        if self._window_backend is None:
            raise UnsupportedBackendError(self._backend_error or "Backend de ventanas no disponible.")

        state = self.state_store.load_state(profile.id)
        new_state = self._window_backend.toggle(profile, state)
        if new_state.pid is None and not new_state.visible:
            self.state_store.clear_state(profile.id)
        else:
            self.state_store.save_state(new_state)
