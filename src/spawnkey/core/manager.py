"""High-level coordinator shared by SpawnKey CLI and GTK GUI."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
from typing import Any

from spawnkey.backends.base import (
    BackendCapabilities,
    BackendError,
    DiagnosticItem,
    ShortcutBackend,
    ShortcutRegistrationStatus,
    WindowBackend,
)
from spawnkey.backends.detector import EnvironmentInfo, detect_environment
from spawnkey.backends.factory import UnsupportedBackendError, get_backends
from spawnkey.core.desktop_apps import (
    discover_desktop_apps,
    validate_executable_command,
    wrap_terminal_command,
)
from spawnkey.core.models import DropdownProfile, MonitorRect, SourceType
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import find_profile_shortcut_conflicts
from spawnkey.core.storage import AppPaths, ProfileStore, RuntimeStateStore, _atomic_write_json
from spawnkey.i18n import _, ngettext


EXPORT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class ManagedEntry:
    """Combined view of a dropdown profile and its live system shortcut state."""

    profile: DropdownProfile
    shortcut_status: ShortcutRegistrationStatus
    script_exists: bool


@dataclass(frozen=True)
class ImportResult:
    """Summary of a profile import operation."""

    imported: list[str]
    updated: list[str]
    skipped: list[str]
    warnings: list[str]


def _replace_home_prefix(value: str, home_prefixes: list[str]) -> str:
    for prefix in home_prefixes:
        if value == prefix:
            return "~"
        if value.startswith(prefix + "/"):
            return "~/" + value[len(prefix) + 1 :]
    return value


def _quote_portable_token(token: str) -> str:
    if token == "~" or token.startswith("~/"):
        rest = token[2:]
        if not rest:
            return token
        quoted_rest = shlex.quote(rest)
        if quoted_rest == rest:
            return token
        return f"~/{quoted_rest}"
    if "=" in token and not token.startswith("="):
        key, val = token.split("=", 1)
        if val == "~" or val.startswith("~/"):
            return f"{shlex.quote(key)}={_quote_portable_token(val)}"
    return shlex.quote(token)


def sanitize_profile_for_export(
    profile: DropdownProfile,
    *,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a portable profile dictionary stripped of host-specific paths and IDs."""
    data = profile.to_dict()
    data.pop("script_path", None)
    data.pop("shortcut_backend_id", None)

    raw_home = home_dir or Path.home()
    home_prefixes: list[str] = []
    for candidate in (str(raw_home), str(raw_home.resolve())):
        norm = candidate.rstrip("/")
        if norm and norm not in home_prefixes:
            home_prefixes.append(norm)
    home_prefixes.sort(key=len, reverse=True)

    exec_cmd = str(data.get("exec_cmd", ""))
    if exec_cmd and home_prefixes:
        try:
            tokens = shlex.split(exec_cmd)
        except ValueError:
            tokens = exec_cmd.split()
        normalized: list[str] = []
        for tok in tokens:
            replaced = _replace_home_prefix(tok, home_prefixes)
            if replaced == tok and "=" in tok and not tok.startswith("="):
                key, val = tok.split("=", 1)
                new_val = _replace_home_prefix(val, home_prefixes)
                if new_val != val:
                    replaced = f"{key}={new_val}"
            normalized.append(_quote_portable_token(replaced))
        data["exec_cmd"] = " ".join(normalized)

    return data


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

    def find_system_shortcut_conflicts(self, shortcut: str) -> list[str]:
        """Return human-readable descriptions of OS shortcuts colliding with the given shortcut."""
        if self._shortcut_backend is None or not (shortcut or "").strip():
            return []
        try:
            return list(self._shortcut_backend.find_system_conflicts(shortcut))
        except Exception:
            return []

    def diagnose(self) -> list[DiagnosticItem]:
        """Aggregate environment, backend, capability, script, and shortcut diagnostics."""
        yes = _("yes")
        no = _("no")
        items: list[DiagnosticItem] = [
            (
                "ok",
                _("session"),
                _("session={session} | desktop={desktop} | backend={backend}").format(
                    session=self.env_info.session_type,
                    desktop=self.env_info.desktop,
                    backend=self.env_info.backend.value,
                ),
            )
        ]

        if not self.backend_available:
            items.append(
                (
                    "error",
                    _("backend"),
                    self._backend_error or _("Could not initialize the active backend."),
                )
            )

        if self._window_backend is not None:
            try:
                ref = DropdownProfile(id="ref", name="Ref", exec_cmd="true")
                mon = self._window_backend.get_active_monitor(ref)
                items.append(
                    (
                        "ok",
                        _("monitor"),
                        f"{mon.name}: {mon.width}x{mon.height} @ ({mon.x}, {mon.y})",
                    )
                )
            except Exception as exc:
                items.append(
                    (
                        "warn",
                        _("monitor"),
                        _("Could not detect active monitor: {error}").format(error=exc),
                    )
                )
        else:
            mon = self.get_active_monitor()
            items.append(
                (
                    "warn",
                    _("monitor"),
                    _("Fallback without backend: {width}x{height}").format(
                        width=mon.width,
                        height=mon.height,
                    ),
                )
            )

        caps = self.capabilities
        items.append(
            (
                "ok",
                _("capabilities"),
                _(
                    "opacity={opacity}, corner_radius={corner_radius}, "
                    "click_outside={click_outside}, animations={animations}"
                ).format(
                    opacity=yes if caps.supports_opacity else no,
                    corner_radius=yes if caps.supports_corner_radius else no,
                    click_outside=yes if caps.supports_click_outside else no,
                    animations=yes if caps.supports_animations else no,
                ),
            )
        )
        if caps.backend_hint:
            items.append(("warn", _("backend_hint"), caps.backend_hint))
        if not caps.supports_corner_radius and caps.corner_radius_hint:
            items.append(("warn", _("corner_radius"), caps.corner_radius_hint))

        profiles = self.store.list_profiles()

        if self._window_backend is not None:
            try:
                items.extend(self._window_backend.diagnose())
            except Exception as exc:
                items.append(("warn", _("window_diagnostics"), str(exc)))

        if self._shortcut_backend is not None:
            try:
                items.extend(self._shortcut_backend.diagnose(profiles))
            except Exception as exc:
                items.append(("warn", _("shortcut_diagnostics"), str(exc)))

        for profile in profiles:
            script_path = (
                Path(profile.script_path)
                if profile.script_path
                else (self.paths.scripts_dir / f"{profile.id}.sh")
            )
            if not script_path.exists():
                items.append(
                    (
                        "error",
                        f"{_('script')}:{profile.id}",
                        _("Script does not exist: {path}").format(path=script_path),
                    )
                )
            elif not os.access(script_path, os.X_OK):
                items.append(
                    (
                        "warn",
                        f"{_('script')}:{profile.id}",
                        _("Missing execute permission (+x): {path}").format(path=script_path),
                    )
                )

            if self._shortcut_backend is not None:
                status = self._shortcut_backend.get_shortcut_status(profile)
                if status is ShortcutRegistrationStatus.OUT_OF_SYNC:
                    items.append(
                        (
                            "warn",
                            f"{_('shortcut')}:{profile.id}",
                            _("Shortcut '{shortcut}' out of sync ({status}).").format(
                                shortcut=profile.shortcut,
                                status=status.value,
                            ),
                        )
                    )

            if profile.shortcut and profile.shortcut_enabled:
                sys_conflicts = self.find_system_shortcut_conflicts(profile.shortcut)
                if sys_conflicts:
                    items.append(
                        (
                            "warn",
                            f"{_('collision')}:{profile.id}",
                            _("'{shortcut}' collides with the system: {details}").format(
                                shortcut=profile.shortcut,
                                details="; ".join(sys_conflicts),
                            ),
                        )
                    )

        orphans = self.list_orphaned_shortcuts()
        if orphans:
            items.append(
                (
                    "warn",
                    _("orphaned_shortcuts"),
                    ngettext(
                        "{count} orphaned shortcut: {items}",
                        "{count} orphaned shortcuts: {items}",
                        len(orphans),
                    ).format(count=len(orphans), items=", ".join(orphans)),
                )
            )
        elif self._shortcut_backend is not None:
            items.append(("ok", _("orphaned_shortcuts"), _("No orphaned shortcuts.")))

        return items

    def export_profiles(self, profile_ids: list[str] | None = None) -> dict[str, Any]:
        """Export all or selected profiles into a portable JSON-serializable bundle."""
        all_profiles = self.store.list_profiles()
        by_id = {p.id: p for p in all_profiles}

        if profile_ids is None:
            selected = all_profiles
        else:
            selected = []
            for pid in profile_ids:
                prof = by_id.get(pid)
                if prof is None:
                    raise ValueError(_("Profile '{id}' not found.").format(id=pid))
                selected.append(prof)

        return {
            "spawnkey_export_version": EXPORT_FORMAT_VERSION,
            "profiles": [sanitize_profile_for_export(p) for p in selected],
        }

    def export_profiles_to_file(
        self,
        output_path: Path,
        profile_ids: list[str] | None = None,
    ) -> Path:
        """Atomically export profiles as portable JSON to output_path."""
        payload = self.export_profiles(profile_ids=profile_ids)
        _atomic_write_json(output_path, payload)
        return output_path

    def import_profiles(
        self,
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        overwrite: bool = False,
        register_shortcuts: bool = True,
    ) -> ImportResult:
        """Import profiles from a portable bundle, list, or single profile dict."""
        raw_items: list[Any]
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            if "profiles" in payload:
                profiles_val = payload["profiles"]
                if not isinstance(profiles_val, list):
                    raise ValueError(
                        _("Invalid import payload: 'profiles' must be a list.")
                    )
                raw_items = profiles_val
            elif any(k in payload for k in ("id", "name", "exec_cmd")):
                raw_items = [payload]
            else:
                raise ValueError(
                    _("Invalid import payload: expected a 'profiles' bundle or profile object.")
                )
        else:
            raise ValueError(
                _("Invalid import payload: expected a JSON object or list of profiles.")
            )

        imported: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        warnings: list[str] = []
        desktop_map: dict[str, Any] | None = None

        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(
                    _("Invalid profile entry at index {index}: expected an object.").format(
                        index=idx
                    )
                )
            cleaned = dict(item)
            cleaned.pop("script_path", None)
            cleaned.pop("shortcut_backend_id", None)
            try:
                prof = DropdownProfile.from_dict(cleaned)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    _("Invalid profile entry at index {index}: {error}").format(
                        index=idx,
                        error=exc,
                    )
                ) from exc

            existed = self.store.get_profile(prof.id) is not None
            if existed and not overwrite:
                skipped.append(prof.id)
                continue

            if prof.source_type is SourceType.DESKTOP and prof.desktop_id:
                if desktop_map is None:
                    desktop_map = {app.desktop_id: app for app in discover_desktop_apps()}
                match = desktop_map.get(prof.desktop_id)
                if match is not None:
                    prof.exec_cmd = (
                        wrap_terminal_command(match.exec_cmd)
                        if match.terminal
                        else match.exec_cmd
                    )
                    prof.match_hint = prof.match_hint or match.wm_class
                else:
                    warnings.append(
                        _("Desktop entry '{desktop_id}' for '{id}' was not found on this system.").format(
                            desktop_id=prof.desktop_id,
                            id=prof.id,
                        )
                    )
            elif prof.source_type is SourceType.EXECUTABLE:
                ok, detail = validate_executable_command(prof.exec_cmd)
                if not ok:
                    warnings.append(
                        _("Executable check warning for '{id}': {detail}").format(
                            id=prof.id,
                            detail=detail,
                        )
                    )

            self.script_gen.generate(prof)

            if register_shortcuts and prof.shortcut and prof.shortcut_enabled:
                if self._shortcut_backend is not None:
                    try:
                        self._shortcut_backend.register_shortcut(prof)
                    except (BackendError, ValueError) as exc:
                        prof.shortcut_enabled = False
                        warnings.append(
                            _("Could not register shortcut '{shortcut}' for '{id}': {error}").format(
                                shortcut=prof.shortcut,
                                id=prof.id,
                                error=exc,
                            )
                        )
                else:
                    prof.shortcut_enabled = False
                    warnings.append(
                        _("No active shortcut backend; saved '{id}' with shortcut disabled.").format(
                            id=prof.id
                        )
                    )

            self.store.save_profile(prof)
            if existed:
                updated.append(prof.id)
            else:
                imported.append(prof.id)

        return ImportResult(
            imported=imported,
            updated=updated,
            skipped=skipped,
            warnings=warnings,
        )

    def import_profiles_from_file(
        self,
        input_path: Path,
        *,
        overwrite: bool = False,
        register_shortcuts: bool = True,
    ) -> ImportResult:
        """Read and import profiles from a JSON file."""
        try:
            raw_text = input_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                _("Could not read import file '{path}': {error}").format(
                    path=input_path,
                    error=exc,
                )
            ) from exc
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                _("Invalid JSON in '{path}': {error}").format(
                    path=input_path,
                    error=exc,
                )
            ) from exc
        return self.import_profiles(
            payload,
            overwrite=overwrite,
            register_shortcuts=register_shortcuts,
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
                    shortcut_msg = _("Shortcut '{shortcut}' active.").format(
                        shortcut=profile.shortcut
                    )
                else:
                    removed = self._shortcut_backend.remove_shortcut(profile)
                    shortcut_msg = (
                        _("Shortcut removed from system.")
                        if removed
                        else _("Saved without shortcut.")
                    )
            except (BackendError, ValueError) as exc:
                profile.shortcut_enabled = False
                self.store.save_profile(profile)
                raise ValueError(
                    _("Profile saved, but shortcut failed: {error}").format(error=exc)
                ) from exc

        self.store.save_profile(profile)
        return profile, shortcut_msg

    def remove_shortcut_only(self, profile_id: str, *, keep_keys: bool = False) -> bool:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise ValueError(_("Profile '{id}' not found.").format(id=profile_id))

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
            raise ValueError(_("Profile '{id}' not found.").format(id=profile_id))
        if keys is not None:
            profile.shortcut = keys
        if not profile.shortcut:
            raise ValueError(_("Assign a key combination first."))
        if self._shortcut_backend is None:
            raise UnsupportedBackendError(
                self._backend_error or _("Shortcut backend unavailable.")
            )

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
            raise ValueError(_("Profile '{id}' not found.").format(id=profile_id))
        if self._window_backend is None:
            raise UnsupportedBackendError(
                self._backend_error or _("Window backend unavailable.")
            )

        state = self.state_store.load_state(profile.id)
        new_state = self._window_backend.toggle(profile, state)
        if new_state.pid is None and not new_state.visible:
            self.state_store.clear_state(profile.id)
        else:
            self.state_store.save_state(new_state)
