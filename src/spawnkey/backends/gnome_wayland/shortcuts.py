"""GNOME Wayland global shortcut backend (gsd-media-keys custom-keybindings + XDG portal sync)."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from spawnkey.backends.base import (
    BackendError,
    ShortcutBackend,
    ShortcutRegistrationStatus,
)
from spawnkey.backends.portal_shortcuts import (
    bind_portal_shortcut_oneshot,
    shortcut_to_xdg_trigger,
)
from spawnkey.core.models import DropdownProfile
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import ParsedShortcut, parse_shortcut as parse_common_shortcut
from spawnkey.core.storage import AppPaths


MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
MEDIA_KEYS_LIST_KEY = "custom-keybindings"
CUSTOM_BINDING_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
CUSTOM_BINDING_BASE_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"

SYSTEM_KEYBINDING_SCHEMAS: tuple[str, ...] = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
)

_GNOME_MODIFIER_MAP: dict[str, str] = {
    "Meta": "<Super>",
    "Ctrl": "<Control>",
    "Alt": "<Alt>",
    "Shift": "<Shift>",
}

_GNOME_REVERSE_MODIFIER_MAP: dict[str, str] = {
    "super": "Meta",
    "meta": "Meta",
    "logo": "Meta",
    "win": "Meta",
    "control": "Ctrl",
    "ctrl": "Ctrl",
    "primary": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}

_GNOME_KEY_MAP: dict[str, str] = {
    "Esc": "Escape",
    "Tab": "Tab",
    "Backspace": "BackSpace",
    "Return": "Return",
    "Ins": "Insert",
    "Del": "Delete",
    "Home": "Home",
    "End": "End",
    "Left": "Left",
    "Up": "Up",
    "Right": "Right",
    "Down": "Down",
    "PgUp": "Page_Up",
    "PgDown": "Page_Down",
    "Space": "space",
    "Print": "Print",
    "Pause": "Pause",
    "Menu": "Menu",
    "KP_Add": "KP_Add",
    "KP_Subtract": "KP_Subtract",
    "KP_Multiply": "KP_Multiply",
    "KP_Divide": "KP_Divide",
    "KP_Decimal": "KP_Decimal",
    "`": "grave",
    "-": "minus",
    "=": "equal",
    "+": "plus",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    "[": "bracketleft",
    "]": "bracketright",
}

for _kp_digit in range(10):
    _GNOME_KEY_MAP[f"KP_{_kp_digit}"] = f"KP_{_kp_digit}"

for _fn_num in range(1, 25):
    _GNOME_KEY_MAP[f"F{_fn_num}"] = f"F{_fn_num}"

_GNOME_REVERSE_KEY_MAP: dict[str, str] = {v.lower(): k for k, v in _GNOME_KEY_MAP.items()}


def shortcut_to_gnome_accel(parsed: ParsedShortcut) -> str:
    """Translate a normalized ParsedShortcut into a GTK/GNOME accelerator string.

    Example:
        Meta+Shift+T -> <Super><Shift>t
        Ctrl+Alt+F12 -> <Control><Alt>F12
    """
    mods = "".join(_GNOME_MODIFIER_MAP[m] for m in parsed.modifiers)
    if parsed.key in _GNOME_KEY_MAP:
        key_sym = _GNOME_KEY_MAP[parsed.key]
    elif len(parsed.key) == 1 and parsed.key.isalpha():
        key_sym = parsed.key.lower()
    else:
        key_sym = parsed.key
    return f"{mods}{key_sym}"


def gnome_accel_to_canonical(accel: str) -> str:
    """Parse a GTK/GNOME accelerator string (e.g. '<Super><Shift>t') to canonical SpawnKey format."""
    text = (accel or "").strip().strip("'\"")
    if not text:
        raise ValueError("El acelerador de GNOME está vacío.")

    mod_tokens = re.findall(r"<([^>]+)>", text)
    key_token = re.sub(r"^(?:<[^>]+>)+", "", text).strip()
    if not key_token:
        raise ValueError(f"Acelerador de GNOME sin tecla principal: {accel!r}")

    parts: list[str] = []
    for mod in mod_tokens:
        low = mod.lower()
        if low not in _GNOME_REVERSE_MODIFIER_MAP:
            raise ValueError(f"Modificador GNOME desconocido: '<{mod}>'")
        parts.append(_GNOME_REVERSE_MODIFIER_MAP[low])

    key_low = key_token.lower()
    if key_low in _GNOME_REVERSE_KEY_MAP:
        parts.append(_GNOME_REVERSE_KEY_MAP[key_low])
    else:
        parts.append(key_token)

    return parse_common_shortcut("+".join(parts)).canonical


def parse_gsettings_strv(raw: str) -> list[str]:
    """Parse a GVariant string array output from `gsettings get` (e.g. \"['a', 'b']\" or '@as []')."""
    cleaned = (raw or "").strip()
    if not cleaned or cleaned == "@as []":
        return []
    if cleaned.startswith("@as "):
        cleaned = cleaned[4:].strip()
    try:
        parsed = ast.literal_eval(cleaned)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    except (ValueError, SyntaxError):
        pass
    return []


def format_gsettings_strv(items: list[str]) -> str:
    """Format a Python string list into a GVariant `as` literal for `gsettings set`."""
    if not items:
        return "@as []"
    escaped = ", ".join(repr(str(x)) for x in items)
    return f"[{escaped}]"


class GnomeWaylandShortcutBackend(ShortcutBackend):
    """Registers, audits, and cleanly removes SpawnKey global shortcuts on GNOME Wayland.

    Uses GNOME's native `org.gnome.settings-daemon.plugins.media-keys` custom-keybindings
    (executed by `gsd-media-keys` with zero resident SpawnKey daemon) and synchronizes
    XDG `.desktop` + `org.freedesktop.portal.GlobalShortcuts` metadata.
    """

    name = "gnome-wayland"

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        applications_dir: Path | None = None,
        store_path: Path | None = None,
        use_gsettings: bool = True,
        use_portal_dbus: bool = False,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        self.applications_dir = applications_dir or (data_home / "applications")
        self.store_path = store_path or (self.paths.config_dir / "gnome_shortcuts.json")
        self.use_gsettings = use_gsettings
        self.use_portal_dbus = use_portal_dbus

    def component_id(self, profile: DropdownProfile) -> str:
        return profile.shortcut_backend_id or f"spawnkey-{profile.id}.desktop"

    @staticmethod
    def _component_slug(component_id: str) -> str:
        if not component_id.startswith("spawnkey-"):
            raise ValueError(
                f"Seguridad: se rechazó operar sobre '{component_id}' porque no pertenece a SpawnKey."
            )
        stem = component_id[:-8] if component_id.endswith(".desktop") else component_id
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", stem)
        return safe

    def dconf_path_for_component(self, component_id: str) -> str:
        slug = self._component_slug(component_id)
        return f"{CUSTOM_BINDING_BASE_PATH}/{slug}/"

    @staticmethod
    def component_from_dconf_path(dconf_path: str) -> str | None:
        norm = dconf_path.strip().rstrip("/")
        prefix = f"{CUSTOM_BINDING_BASE_PATH}/"
        if not norm.startswith(prefix):
            return None
        slug = norm[len(prefix) :]
        if not slug.startswith("spawnkey-"):
            return None
        return f"{slug}.desktop" if not slug.endswith(".desktop") else slug

    def desktop_file_path(self, component_id: str) -> Path:
        return self.applications_dir / component_id

    def _is_run_gsettings_patched(self) -> bool:
        if "_run_gsettings" in self.__dict__:
            return True
        return type(self)._run_gsettings is not GnomeWaylandShortcutBackend._run_gsettings

    def _get_gio(self) -> Any | None:
        """Lazily import Gio when gsettings is enabled and not mocked out."""
        if not self.use_gsettings or self._is_run_gsettings_patched():
            return None
        if not shutil.which("gsettings"):
            return None
        try:
            from gi.repository import Gio  # type: ignore[import-not-found]

            source = Gio.SettingsSchemaSource.get_default()
            if source is None or source.lookup(MEDIA_KEYS_SCHEMA, True) is None:
                return None
            return Gio
        except Exception:
            return None

    @staticmethod
    def _gio_open(gio: Any, schema_id: str, path: str | None = None) -> Any | None:
        source = gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup(schema_id, True) is None:
            return None
        if path:
            return gio.Settings.new_with_path(schema_id, path)
        return gio.Settings.new(schema_id)

    def _run_gsettings(self, args: list[str], *, check: bool = True) -> str:
        gsettings = shutil.which("gsettings")
        if not gsettings:
            raise BackendError("No se encontró 'gsettings' en PATH para administrar atajos en GNOME.")
        proc = subprocess.run(
            [gsettings, *args],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise BackendError(f"Error de gsettings ({' '.join(args)}): {err}")
        return proc.stdout.strip()

    def _gsettings_get_strv(self, schema_id: str, key: str, *, check: bool = True) -> list[str]:
        gio = self._get_gio()
        if gio is not None:
            settings = self._gio_open(gio, schema_id)
            if settings is not None:
                return [str(x) for x in settings.get_strv(key) if x]
        raw = self._run_gsettings(["get", schema_id, key], check=check)
        return parse_gsettings_strv(raw)

    def _gsettings_set_strv(self, schema_id: str, key: str, values: list[str], *, check: bool = True) -> None:
        gio = self._get_gio()
        if gio is not None:
            settings = self._gio_open(gio, schema_id)
            if settings is not None:
                settings.set_strv(key, values)
                gio.Settings.sync()
                return
        self._run_gsettings(["set", schema_id, key, format_gsettings_strv(values)], check=check)

    def _gsettings_get_custom(self, dconf_path: str, key: str) -> str:
        gio = self._get_gio()
        if gio is not None:
            settings = self._gio_open(gio, CUSTOM_BINDING_SCHEMA, dconf_path)
            if settings is not None:
                return str(settings.get_string(key))
        item_schema = f"{CUSTOM_BINDING_SCHEMA}:{dconf_path}"
        return self._run_gsettings(["get", item_schema, key], check=False).strip("'\"")

    def _gsettings_set_custom(self, dconf_path: str, values: dict[str, str]) -> None:
        gio = self._get_gio()
        if gio is not None:
            settings = self._gio_open(gio, CUSTOM_BINDING_SCHEMA, dconf_path)
            if settings is not None:
                for key, val in values.items():
                    settings.set_string(key, val)
                gio.Settings.sync()
                return
        item_schema = f"{CUSTOM_BINDING_SCHEMA}:{dconf_path}"
        for key, val in values.items():
            self._run_gsettings(["set", item_schema, key, val])

    def _gsettings_reset_custom(self, dconf_path: str, keys: tuple[str, ...]) -> None:
        gio = self._get_gio()
        if gio is not None:
            settings = self._gio_open(gio, CUSTOM_BINDING_SCHEMA, dconf_path)
            if settings is not None:
                for key in keys:
                    settings.reset(key)
                gio.Settings.sync()
                return
        item_schema = f"{CUSTOM_BINDING_SCHEMA}:{dconf_path}"
        for key in keys:
            self._run_gsettings(["reset", item_schema, key], check=False)

    def _load_file_store(self) -> dict[str, dict[str, str]]:
        if not self.store_path.exists():
            return {}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
                return dict(data["bindings"])
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_file_store(self, bindings: dict[str, dict[str, str]]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"bindings": bindings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.store_path)

    def _write_desktop_entry(self, profile: DropdownProfile, component: str, xdg_trigger: str) -> Path:
        script_path = profile.script_path
        if not script_path or not Path(script_path).exists():
            script_path = str(ScriptGenerator(self.paths).generate(profile))

        self.applications_dir.mkdir(parents=True, exist_ok=True)
        target = self.desktop_file_path(component)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name=SpawnKey: {profile.name}\n"
            f"Comment=Dropdown administrado por SpawnKey ({profile.id})\n"
            f"Exec={script_path}\n"
            "Icon=preferences-desktop-keyboard-shortcuts\n"
            "NoDisplay=true\n"
            "StartupNotify=false\n"
            f"X-SpawnKey-Profile={profile.id}\n"
            f"X-SpawnKey-XDG-Trigger={xdg_trigger}\n"
        )
        target.write_text(content, encoding="utf-8")
        return target

    def register_shortcut(self, profile: DropdownProfile) -> None:
        if not profile.shortcut or not profile.shortcut.strip():
            raise ValueError(f"El perfil '{profile.id}' no tiene una combinación de teclas asignada.")

        parsed = parse_common_shortcut(profile.shortcut)
        accel = shortcut_to_gnome_accel(parsed)
        xdg_trigger = shortcut_to_xdg_trigger(parsed)
        component = self.component_id(profile)
        dconf_path = self.dconf_path_for_component(component)

        if not profile.script_path or not Path(profile.script_path).exists():
            ScriptGenerator(self.paths).generate(profile)

        self._write_desktop_entry(profile, component, xdg_trigger)

        if self.use_gsettings:
            # Populate name/command/binding BEFORE updating custom-keybindings so
            # gsd-media-keys immediately sees the populated entry when notified.
            self._gsettings_set_custom(
                dconf_path,
                {
                    "name": f"SpawnKey: {profile.name}",
                    "command": profile.script_path,
                    "binding": accel,
                },
            )
            current_paths = self._gsettings_get_strv(MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY)
            if dconf_path not in current_paths:
                current_paths.append(dconf_path)
                self._gsettings_set_strv(MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, current_paths)
            else:
                # Re-notify gsd-media-keys in case the path previously existed with empty fields
                without_path = [p for p in current_paths if p != dconf_path]
                self._gsettings_set_strv(MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, without_path)
                self._gsettings_set_strv(MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, current_paths)

        bindings = self._load_file_store()
        bindings[component] = {
            "dconf_path": dconf_path,
            "name": f"SpawnKey: {profile.name}",
            "command": profile.script_path,
            "binding": accel,
            "canonical": parsed.canonical,
            "xdg_trigger": xdg_trigger,
        }
        self._save_file_store(bindings)

        if self.use_portal_dbus:
            bind_portal_shortcut_oneshot(profile, xdg_trigger)

        profile.shortcut = parsed.canonical
        profile.shortcut_enabled = True
        profile.shortcut_backend_id = component

    def remove_shortcut(self, profile: DropdownProfile) -> bool:
        component = self.component_id(profile)
        return self.remove_shortcut_by_component(component)

    def remove_shortcut_by_component(self, component_id: str) -> bool:
        dconf_path = self.dconf_path_for_component(component_id)
        removed_any = False

        if self.use_gsettings and shutil.which("gsettings"):
            current_paths = self._gsettings_get_strv(
                MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, check=False
            )
            if dconf_path in current_paths:
                updated = [p for p in current_paths if p != dconf_path]
                self._gsettings_set_strv(
                    MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, updated, check=False
                )
                removed_any = True
            self._gsettings_reset_custom(dconf_path, ("name", "command", "binding"))

        desktop_file = self.desktop_file_path(component_id)
        if desktop_file.exists():
            desktop_file.unlink()
            removed_any = True

        bindings = self._load_file_store()
        if component_id in bindings:
            bindings.pop(component_id, None)
            self._save_file_store(bindings)
            removed_any = True

        return removed_any

    def get_shortcut_status(self, profile: DropdownProfile) -> ShortcutRegistrationStatus:
        if not profile.shortcut or not profile.shortcut.strip():
            return ShortcutRegistrationStatus.UNASSIGNED
        if not profile.shortcut_enabled:
            return ShortcutRegistrationStatus.DISABLED

        component = self.component_id(profile)
        desktop_file = self.desktop_file_path(component)
        if not desktop_file.exists():
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        try:
            expected = parse_common_shortcut(profile.shortcut)
            expected_accel = shortcut_to_gnome_accel(expected)
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        dconf_path = self.dconf_path_for_component(component)

        if self.use_gsettings and shutil.which("gsettings"):
            current_paths = self._gsettings_get_strv(
                MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, check=False
            )
            if dconf_path not in current_paths:
                return ShortcutRegistrationStatus.OUT_OF_SYNC
            raw_binding = self._gsettings_get_custom(dconf_path, "binding")
            raw_cmd = self._gsettings_get_custom(dconf_path, "command")
            try:
                if gnome_accel_to_canonical(raw_binding) != expected.canonical:
                    return ShortcutRegistrationStatus.OUT_OF_SYNC
            except ValueError:
                return ShortcutRegistrationStatus.OUT_OF_SYNC
            if profile.script_path and raw_cmd != profile.script_path:
                return ShortcutRegistrationStatus.OUT_OF_SYNC
            return ShortcutRegistrationStatus.ACTIVE

        bindings = self._load_file_store()
        entry = bindings.get(component)
        if not entry:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if entry.get("binding") != expected_accel:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if profile.script_path and entry.get("command") != profile.script_path:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        return ShortcutRegistrationStatus.ACTIVE

    def list_managed_shortcut_ids(self) -> list[str]:
        found: set[str] = set()
        if self.applications_dir.is_dir():
            for entry in self.applications_dir.glob("spawnkey-*.desktop"):
                found.add(entry.name)
        for comp in self._load_file_store():
            if comp.startswith("spawnkey-"):
                found.add(comp)
        if self.use_gsettings and shutil.which("gsettings"):
            for dconf_path in self._gsettings_get_strv(
                MEDIA_KEYS_SCHEMA, MEDIA_KEYS_LIST_KEY, check=False
            ):
                comp = self.component_from_dconf_path(dconf_path)
                if comp:
                    found.add(comp)
        return sorted(found)

    def _read_schema_keybindings(self, schema: str) -> dict[str, list[str]]:
        """Read all string-array accelerator keys from a GNOME keybinding schema."""
        gio = self._get_gio()
        if gio is not None:
            source = gio.SettingsSchemaSource.get_default()
            schema_obj = source.lookup(schema, True) if source is not None else None
            if schema_obj is None:
                return {}
            settings = gio.Settings.new(schema)
            out: dict[str, list[str]] = {}
            for key in schema_obj.list_keys():
                try:
                    variant = settings.get_value(key)
                    if variant.get_type_string() != "as":
                        continue
                    out[key] = [str(x) for x in settings.get_strv(key) if x]
                except Exception:
                    continue
            return out

        if not self._is_run_gsettings_patched() and not shutil.which("gsettings"):
            return {}

        out = {}
        raw = self._run_gsettings(["list-recursively", schema], check=False)
        for line in raw.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0] == schema:
                out[parts[1]] = parse_gsettings_strv(parts[2])
        if out:
            return out

        keys_raw = self._run_gsettings(["list-keys", schema], check=False)
        for key in (k.strip() for k in keys_raw.splitlines() if k.strip()):
            val_raw = self._run_gsettings(["get", schema, key], check=False)
            accels = parse_gsettings_strv(val_raw)
            if accels:
                out[key] = accels
        return out

    def find_system_conflicts(self, shortcut: str) -> list[str]:
        """Return GNOME system keybinding keys (schema:key) colliding with shortcut."""
        if not shortcut or not shortcut.strip() or not self.use_gsettings:
            return []
        try:
            target = parse_common_shortcut(shortcut).canonical
        except ValueError:
            return []

        conflicts: list[str] = []
        seen: set[str] = set()
        for schema in SYSTEM_KEYBINDING_SCHEMAS:
            for key, accels in self._read_schema_keybindings(schema).items():
                for accel in accels:
                    cleaned = accel.strip()
                    if not cleaned or cleaned.lower() in ("disabled", "none"):
                        continue
                    try:
                        canon = gnome_accel_to_canonical(cleaned)
                    except ValueError:
                        continue
                    if canon == target:
                        label = f"{schema}:{key}"
                        if label not in seen:
                            seen.add(label)
                            conflicts.append(label)
                        break
        return conflicts

    def diagnose(self, profiles: list[DropdownProfile] | None = None) -> list[tuple[str, str, str]]:
        """Run non-destructive diagnostics for GNOME Wayland shortcut registration."""
        results: list[tuple[str, str, str]] = []

        gio = self._get_gio()
        has_cli = bool(shutil.which("gsettings"))
        if gio is not None:
            results.append(
                (
                    "ok",
                    "Backend de atajos GNOME",
                    "Gio.Settings disponible en proceso con esquema media-keys.",
                )
            )
        elif has_cli:
            results.append(
                (
                    "ok",
                    "Backend de atajos GNOME",
                    "CLI gsettings disponible en PATH.",
                )
            )
        else:
            results.append(
                (
                    "error",
                    "Backend de atajos GNOME",
                    "No se encontró 'gsettings' ni Gio.Settings para registrar atajos en GNOME.",
                )
            )

        if profiles:
            conflict_found = False
            enabled_count = 0
            for profile in profiles:
                if not profile.shortcut or not profile.shortcut.strip() or not profile.shortcut_enabled:
                    continue
                enabled_count += 1
                conflicts = self.find_system_conflicts(profile.shortcut)
                if conflicts:
                    conflict_found = True
                    results.append(
                        (
                            "warn",
                            f"Conflicto de atajo ({profile.name})",
                            f"'{profile.shortcut}' también está asignado en GNOME a: {', '.join(conflicts)}.",
                        )
                    )
                status = self.get_shortcut_status(profile)
                if status is ShortcutRegistrationStatus.OUT_OF_SYNC:
                    results.append(
                        (
                            "warn",
                            f"Sincronización ({profile.name})",
                            f"El atajo '{profile.shortcut}' no está sincronizado con gsd-media-keys.",
                        )
                    )
            if enabled_count > 0 and not conflict_found:
                results.append(
                    (
                        "ok",
                        "Conflictos de atajos GNOME",
                        f"Sin colisiones del sistema para {enabled_count} perfil(es) activo(s).",
                    )
                )

        return results
