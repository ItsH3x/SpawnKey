"""Universal X11 ShortcutBackend supporting GNOME/KDE delegation, Cinnamon, MATE, XFCE, and generic WMs."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any, Mapping

from spawnkey.backends.base import (
    BackendError,
    ShortcutBackend,
    ShortcutRegistrationStatus,
)
from spawnkey.core.models import DropdownProfile
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import ParsedShortcut, parse_shortcut as parse_common_shortcut
from spawnkey.core.storage import AppPaths


_CINNAMON_SCHEMA = "org.cinnamon.desktop.keybindings"
_CINNAMON_LIST_KEY = "custom-list"
_CINNAMON_ITEM_SCHEMA = "org.cinnamon.desktop.keybindings.custom-keybinding"
_CINNAMON_BASE_PATH = "/org/cinnamon/desktop/keybindings/custom-keybindings"

_MATE_GLOBAL_SCHEMA = "org.mate.Marco.global-keybindings"
_MATE_COMMANDS_SCHEMA = "org.mate.Marco.keybinding-commands"
_MATE_SLOT_COUNT = 12

_XFCE_CHANNEL = "xfce4-keyboard-shortcuts"

_GTK_MODIFIERS: dict[str, str] = {
    "Meta": "<Super>",
    "Ctrl": "<Control>",
    "Alt": "<Alt>",
    "Shift": "<Shift>",
}

_XFCE_MODIFIERS: dict[str, str] = {
    "Meta": "<Super>",
    "Ctrl": "<Primary>",
    "Alt": "<Alt>",
    "Shift": "<Shift>",
}

_XBINDKEYS_MODIFIERS: dict[str, str] = {
    "Meta": "Mod4",
    "Ctrl": "Control",
    "Alt": "Mod1",
    "Shift": "Shift",
}

_X11_KEYSYM_MAP: dict[str, str] = {
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


def _x11_keysym(canonical_key: str) -> str:
    if canonical_key in _X11_KEYSYM_MAP:
        return _X11_KEYSYM_MAP[canonical_key]
    if len(canonical_key) == 1 and canonical_key.isalpha():
        return canonical_key.lower()
    return canonical_key


def shortcut_to_x11_accel(parsed: ParsedShortcut, style: str = "gtk") -> str:
    """Translate a ParsedShortcut to X11 accelerator format ('gtk', 'xfce', or 'xbindkeys')."""
    keysym = _x11_keysym(parsed.key)
    mode = style.strip().lower()
    if mode == "gtk":
        mods = "".join(_GTK_MODIFIERS[m] for m in parsed.modifiers)
        return f"{mods}{keysym}"
    if mode == "xfce":
        mods = "".join(_XFCE_MODIFIERS[m] for m in parsed.modifiers)
        return f"{mods}{keysym}"
    if mode == "xbindkeys":
        parts = [_XBINDKEYS_MODIFIERS[m] for m in parsed.modifiers]
        parts.append(keysym)
        return "+".join(parts)
    raise ValueError(f"Estilo de acelerador X11 desconocido: {style!r}")


def _parse_gsettings_strv(raw: str) -> list[str]:
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


def _format_gsettings_strv(items: list[str]) -> str:
    if not items:
        return "@as []"
    return "[" + ", ".join(repr(str(x)) for x in items) + "]"


def _detect_x11_desktop_kind(
    desktop: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    source = env if env is not None else os.environ
    raw = (
        desktop
        if desktop is not None
        else (source.get("XDG_CURRENT_DESKTOP") or source.get("DESKTOP_SESSION") or "")
    )
    tokens = {tok.strip().lower() for tok in raw.split(":") if tok.strip()}
    if tokens & {"gnome", "budgie", "unity", "ubuntu"}:
        return "gnome"
    if tokens & {"kde", "plasma"}:
        return "kde"
    if "cinnamon" in tokens or "x-cinnamon" in tokens:
        return "cinnamon"
    if "mate" in tokens:
        return "mate"
    if "xfce" in tokens:
        return "xfce"
    if "i3" in tokens:
        return "i3"
    return "generic"


class X11ShortcutBackend(ShortcutBackend):
    """Global shortcut backend for X11 desktops without any resident SpawnKey daemon."""

    name = "x11"

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        desktop: str | None = None,
        env: Mapping[str, str] | None = None,
        applications_dir: Path | None = None,
        store_path: Path | None = None,
        config_home: Path | None = None,
        use_system_tools: bool = True,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        source_env = env if env is not None else os.environ
        data_home = Path(source_env.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        resolved_config_home = config_home or Path(
            source_env.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        )
        self.applications_dir = applications_dir or (data_home / "applications")
        self.store_path = store_path or (self.paths.config_dir / "x11_shortcuts.json")
        self.config_home = resolved_config_home
        self.use_system_tools = use_system_tools
        self.desktop_kind = _detect_x11_desktop_kind(desktop, source_env)
        self._delegate: ShortcutBackend | None = None

    def _get_delegate(self) -> ShortcutBackend | None:
        if self._delegate is not None:
            return self._delegate
        if self.desktop_kind == "gnome":
            from spawnkey.backends.gnome_wayland.shortcuts import GnomeWaylandShortcutBackend

            self._delegate = GnomeWaylandShortcutBackend(
                self.paths,
                applications_dir=self.applications_dir,
                use_gsettings=self.use_system_tools,
                use_portal_dbus=False,
            )
            return self._delegate
        if self.desktop_kind == "kde":
            from spawnkey.backends.kde_wayland.shortcuts import KDEWaylandShortcutBackend

            self._delegate = KDEWaylandShortcutBackend(
                self.paths,
                applications_dir=self.applications_dir,
                kglobalshortcutsrc_path=self.config_home / "kglobalshortcutsrc",
                use_dbus=self.use_system_tools,
            )
            return self._delegate
        return None

    def component_id(self, profile: DropdownProfile) -> str:
        return profile.shortcut_backend_id or f"spawnkey-{profile.id}.desktop"

    def desktop_file_path(self, component_id: str) -> Path:
        return self.applications_dir / component_id

    @staticmethod
    def _validate_component_id(component_id: str) -> str:
        if not component_id.startswith("spawnkey-"):
            raise ValueError(
                f"Seguridad: se rechazó operar sobre '{component_id}' porque no pertenece a SpawnKey."
            )
        stem = component_id[:-8] if component_id.endswith(".desktop") else component_id
        return re.sub(r"[^a-zA-Z0-9_-]", "-", stem)

    def _load_store(self) -> dict[str, dict[str, Any]]:
        if not self.store_path.exists():
            return {}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
                return dict(data["bindings"])
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_store(self, bindings: dict[str, dict[str, Any]]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"bindings": bindings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.store_path)

    def _run_tool(
        self,
        binary: str,
        args: list[str],
        *,
        check: bool = True,
    ) -> str:
        exe = shutil.which(binary)
        if not exe:
            raise BackendError(
                f"No se encontró '{binary}' en PATH para administrar atajos en {self.desktop_kind}."
            )
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise BackendError(f"Error de {binary} ({' '.join(args)}): {err}")
        return proc.stdout.strip()

    def _write_desktop_entry(self, profile: DropdownProfile, component: str, accel: str) -> Path:
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
            f"X-SpawnKey-X11-Accel={accel}\n"
        )
        target.write_text(content, encoding="utf-8")
        return target

    def _accel_style(self) -> str:
        if self.desktop_kind == "xfce":
            return "xfce"
        if self.desktop_kind in ("cinnamon", "mate"):
            return "gtk"
        return "xbindkeys"

    def _register_cinnamon(
        self,
        profile: DropdownProfile,
        slug: str,
        accel: str,
    ) -> dict[str, Any]:
        dconf_path = f"{_CINNAMON_BASE_PATH}/{slug}/"
        if self.use_system_tools:
            raw_list = self._run_tool("gsettings", ["get", _CINNAMON_SCHEMA, _CINNAMON_LIST_KEY])
            current = _parse_gsettings_strv(raw_list)
            if slug not in current:
                current.append(slug)
                self._run_tool(
                    "gsettings",
                    ["set", _CINNAMON_SCHEMA, _CINNAMON_LIST_KEY, _format_gsettings_strv(current)],
                )
            item_schema = f"{_CINNAMON_ITEM_SCHEMA}:{dconf_path}"
            self._run_tool("gsettings", ["set", item_schema, "name", f"SpawnKey: {profile.name}"])
            self._run_tool("gsettings", ["set", item_schema, "command", profile.script_path])
            self._run_tool(
                "gsettings",
                ["set", item_schema, "binding", _format_gsettings_strv([accel])],
            )
        return {"dconf_path": dconf_path, "cinnamon_slug": slug}

    def _remove_cinnamon(self, component_id: str, entry: dict[str, Any] | None) -> bool:
        slug = str((entry or {}).get("cinnamon_slug") or self._validate_component_id(component_id))
        dconf_path = str((entry or {}).get("dconf_path") or f"{_CINNAMON_BASE_PATH}/{slug}/")
        removed = False
        if self.use_system_tools and shutil.which("gsettings"):
            raw_list = self._run_tool(
                "gsettings", ["get", _CINNAMON_SCHEMA, _CINNAMON_LIST_KEY], check=False
            )
            current = _parse_gsettings_strv(raw_list)
            if slug in current:
                updated = [item for item in current if item != slug]
                self._run_tool(
                    "gsettings",
                    ["set", _CINNAMON_SCHEMA, _CINNAMON_LIST_KEY, _format_gsettings_strv(updated)],
                    check=False,
                )
                removed = True
            item_schema = f"{_CINNAMON_ITEM_SCHEMA}:{dconf_path}"
            for key in ("name", "command", "binding"):
                self._run_tool("gsettings", ["reset", item_schema, key], check=False)
        return removed

    def _allocate_mate_slot(
        self,
        component: str,
        bindings: dict[str, dict[str, Any]],
    ) -> int:
        existing_entry = bindings.get(component) or {}
        if isinstance(existing_entry.get("mate_slot"), int):
            slot = int(existing_entry["mate_slot"])
            if 1 <= slot <= _MATE_SLOT_COUNT:
                return slot

        claimed = {
            int(val["mate_slot"])
            for comp, val in bindings.items()
            if comp != component and isinstance(val.get("mate_slot"), int)
        }
        for slot in range(1, _MATE_SLOT_COUNT + 1):
            if slot in claimed:
                continue
            if self.use_system_tools:
                cmd_val = self._run_tool(
                    "gsettings",
                    ["get", _MATE_COMMANDS_SCHEMA, f"command-{slot}"],
                    check=False,
                ).strip("'\"")
                bind_val = self._run_tool(
                    "gsettings",
                    ["get", _MATE_GLOBAL_SCHEMA, f"run-command-{slot}"],
                    check=False,
                ).strip("'\"")
                if cmd_val and cmd_val != "''":
                    continue
                if bind_val and bind_val.lower() not in ("disabled", "''"):
                    continue
            return slot
        raise BackendError(
            "No quedan slots libres en MATE Marco (command-1..12) sin sobrescribir atajos del usuario."
        )

    def _register_mate(
        self,
        profile: DropdownProfile,
        component: str,
        accel: str,
        bindings: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        slot = self._allocate_mate_slot(component, bindings)
        if self.use_system_tools:
            self._run_tool(
                "gsettings",
                ["set", _MATE_COMMANDS_SCHEMA, f"command-{slot}", profile.script_path],
            )
            self._run_tool(
                "gsettings",
                ["set", _MATE_GLOBAL_SCHEMA, f"run-command-{slot}", accel],
            )
        return {"mate_slot": slot}

    def _remove_mate(self, entry: dict[str, Any] | None) -> bool:
        if not entry or not isinstance(entry.get("mate_slot"), int):
            return False
        slot = int(entry["mate_slot"])
        if not (1 <= slot <= _MATE_SLOT_COUNT):
            return False
        if self.use_system_tools and shutil.which("gsettings"):
            self._run_tool(
                "gsettings", ["set", _MATE_COMMANDS_SCHEMA, f"command-{slot}", ""], check=False
            )
            self._run_tool(
                "gsettings",
                ["set", _MATE_GLOBAL_SCHEMA, f"run-command-{slot}", "disabled"],
                check=False,
            )
        return True

    def _register_xfce(
        self,
        profile: DropdownProfile,
        accel: str,
        old_entry: dict[str, Any] | None,
    ) -> dict[str, Any]:
        prop_path = f"/commands/custom/{accel}"
        old_prop = str((old_entry or {}).get("xfce_property") or "")
        if self.use_system_tools:
            if old_prop and old_prop != prop_path:
                self._run_tool(
                    "xfconf-query",
                    ["-c", _XFCE_CHANNEL, "-p", old_prop, "-r"],
                    check=False,
                )
            # Ensure Xfce enables custom command shortcuts in xfsettingsd
            self._run_tool(
                "xfconf-query",
                ["-c", _XFCE_CHANNEL, "-p", "/commands/custom/override", "-n", "-t", "bool", "-s", "true"],
                check=False,
            )
            self._run_tool(
                "xfconf-query",
                ["-c", _XFCE_CHANNEL, "-p", prop_path, "-n", "-t", "string", "-s", profile.script_path],
            )
        return {"xfce_property": prop_path}

    def _remove_xfce(self, entry: dict[str, Any] | None) -> bool:
        prop_path = str((entry or {}).get("xfce_property") or "")
        if not prop_path or not prop_path.startswith("/commands/custom/"):
            return False
        if self.use_system_tools and shutil.which("xfconf-query"):
            self._run_tool(
                "xfconf-query",
                ["-c", _XFCE_CHANNEL, "-p", prop_path, "-r"],
                check=False,
            )
        return True

    def _sync_generic_wm_files(self, bindings: dict[str, dict[str, Any]]) -> None:
        xbind_path = self.config_home / "spawnkey" / "xbindkeysrc.spawnkey"
        xbind_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Managed by SpawnKey — do not edit manually"]
        for comp in sorted(bindings):
            item = bindings[comp]
            cmd = str(item.get("command") or "")
            xbind_accel = str(item.get("xbindkeys_accel") or "")
            if cmd and xbind_accel:
                lines.append(f'"{cmd}"')
                lines.append(f"  {xbind_accel}")
        xbind_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        i3_config = self.config_home / "i3" / "config"
        if i3_config.exists():
            i3_binds = self.config_home / "i3" / "spawnkey-binds.conf"
            i3_lines = ["# Managed by SpawnKey — include this file from ~/.config/i3/config"]
            for comp in sorted(bindings):
                item = bindings[comp]
                cmd = str(item.get("command") or "")
                xbind_accel = str(item.get("xbindkeys_accel") or "")
                if cmd and xbind_accel:
                    i3_lines.append(f"bindsym {xbind_accel} exec --no-startup-id {shlex.quote(cmd)}")
            i3_binds.write_text("\n".join(i3_lines) + "\n", encoding="utf-8")
            if self.use_system_tools and os.environ.get("I3SOCK") and shutil.which("i3-msg"):
                self._run_tool("i3-msg", ["reload"], check=False)

    def register_shortcut(self, profile: DropdownProfile) -> None:
        """Register or update an X11 global shortcut for the active desktop environment."""
        delegate = self._get_delegate()
        if delegate is not None:
            delegate.register_shortcut(profile)
            return

        if not profile.shortcut or not profile.shortcut.strip():
            raise ValueError(f"El perfil '{profile.id}' no tiene una combinación de teclas asignada.")

        parsed = parse_common_shortcut(profile.shortcut)
        accel = shortcut_to_x11_accel(parsed, style=self._accel_style())
        xbind_accel = shortcut_to_x11_accel(parsed, style="xbindkeys")
        component = self.component_id(profile)
        slug = self._validate_component_id(component)

        if not profile.script_path or not Path(profile.script_path).exists():
            ScriptGenerator(self.paths).generate(profile)

        self._write_desktop_entry(profile, component, accel)

        bindings = self._load_store()
        old_entry = bindings.get(component)
        extra: dict[str, Any] = {}
        if self.desktop_kind == "cinnamon":
            extra = self._register_cinnamon(profile, slug, accel)
        elif self.desktop_kind == "mate":
            extra = self._register_mate(profile, component, accel, bindings)
        elif self.desktop_kind == "xfce":
            extra = self._register_xfce(profile, accel, old_entry)

        bindings[component] = {
            "desktop": self.desktop_kind,
            "name": f"SpawnKey: {profile.name}",
            "command": profile.script_path,
            "accel": accel,
            "xbindkeys_accel": xbind_accel,
            "canonical": parsed.canonical,
            **extra,
        }
        self._save_store(bindings)

        if self.desktop_kind not in ("cinnamon", "mate", "xfce"):
            self._sync_generic_wm_files(bindings)

        profile.shortcut = parsed.canonical
        profile.shortcut_enabled = True
        profile.shortcut_backend_id = component

    def remove_shortcut(self, profile: DropdownProfile) -> bool:
        delegate = self._get_delegate()
        if delegate is not None:
            return delegate.remove_shortcut(profile)
        return self.remove_shortcut_by_component(self.component_id(profile))

    def remove_shortcut_by_component(self, component_id: str) -> bool:
        delegate = self._get_delegate()
        if delegate is not None:
            return delegate.remove_shortcut_by_component(component_id)

        self._validate_component_id(component_id)
        bindings = self._load_store()
        entry = bindings.get(component_id)
        removed_any = False

        if self.desktop_kind == "cinnamon":
            removed_any = self._remove_cinnamon(component_id, entry) or removed_any
        elif self.desktop_kind == "mate":
            removed_any = self._remove_mate(entry) or removed_any
        elif self.desktop_kind == "xfce":
            removed_any = self._remove_xfce(entry) or removed_any

        desktop_file = self.desktop_file_path(component_id)
        if desktop_file.exists():
            desktop_file.unlink()
            removed_any = True

        if component_id in bindings:
            bindings.pop(component_id, None)
            self._save_store(bindings)
            removed_any = True

        if self.desktop_kind not in ("cinnamon", "mate", "xfce"):
            self._sync_generic_wm_files(bindings)

        return removed_any

    def get_shortcut_status(self, profile: DropdownProfile) -> ShortcutRegistrationStatus:
        delegate = self._get_delegate()
        if delegate is not None:
            return delegate.get_shortcut_status(profile)

        if not profile.shortcut or not profile.shortcut.strip():
            return ShortcutRegistrationStatus.UNASSIGNED
        if not profile.shortcut_enabled:
            return ShortcutRegistrationStatus.DISABLED

        component = self.component_id(profile)
        if not self.desktop_file_path(component).exists():
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        try:
            expected = parse_common_shortcut(profile.shortcut)
            expected_accel = shortcut_to_x11_accel(expected, style=self._accel_style())
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        bindings = self._load_store()
        entry = bindings.get(component)
        if not entry:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if entry.get("canonical") != expected.canonical or entry.get("accel") != expected_accel:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if profile.script_path and entry.get("command") != profile.script_path:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        if self.use_system_tools and self.desktop_kind == "xfce" and shutil.which("xfconf-query"):
            prop_path = str(entry.get("xfce_property") or f"/commands/custom/{expected_accel}")
            val = self._run_tool(
                "xfconf-query",
                ["-c", _XFCE_CHANNEL, "-p", prop_path],
                check=False,
            )
            if not val or (profile.script_path and val != profile.script_path):
                return ShortcutRegistrationStatus.OUT_OF_SYNC

        if self.use_system_tools and self.desktop_kind == "cinnamon" and shutil.which("gsettings"):
            slug = str(entry.get("cinnamon_slug") or self._validate_component_id(component))
            raw_list = self._run_tool(
                "gsettings", ["get", _CINNAMON_SCHEMA, _CINNAMON_LIST_KEY], check=False
            )
            if slug not in _parse_gsettings_strv(raw_list):
                return ShortcutRegistrationStatus.OUT_OF_SYNC
            dconf_path = str(entry.get("dconf_path") or f"{_CINNAMON_BASE_PATH}/{slug}/")
            item_schema = f"{_CINNAMON_ITEM_SCHEMA}:{dconf_path}"
            raw_bind = self._run_tool("gsettings", ["get", item_schema, "binding"], check=False)
            if expected_accel not in _parse_gsettings_strv(raw_bind):
                return ShortcutRegistrationStatus.OUT_OF_SYNC

        if self.use_system_tools and self.desktop_kind == "mate" and shutil.which("gsettings"):
            slot = entry.get("mate_slot")
            if not isinstance(slot, int):
                return ShortcutRegistrationStatus.OUT_OF_SYNC
            cur_bind = self._run_tool(
                "gsettings", ["get", _MATE_GLOBAL_SCHEMA, f"run-command-{slot}"], check=False
            ).strip("'\"")
            cur_cmd = self._run_tool(
                "gsettings", ["get", _MATE_COMMANDS_SCHEMA, f"command-{slot}"], check=False
            ).strip("'\"")
            if cur_bind != expected_accel:
                return ShortcutRegistrationStatus.OUT_OF_SYNC
            if profile.script_path and cur_cmd != profile.script_path:
                return ShortcutRegistrationStatus.OUT_OF_SYNC

        return ShortcutRegistrationStatus.ACTIVE

    def list_managed_shortcut_ids(self) -> list[str]:
        delegate = self._get_delegate()
        if delegate is not None:
            return delegate.list_managed_shortcut_ids()

        found: set[str] = set()
        if self.applications_dir.is_dir():
            for entry in self.applications_dir.glob("spawnkey-*.desktop"):
                found.add(entry.name)
        for comp in self._load_store():
            if comp.startswith("spawnkey-"):
                found.add(comp)

        if self.use_system_tools and self.desktop_kind == "cinnamon" and shutil.which("gsettings"):
            raw_list = self._run_tool(
                "gsettings", ["get", _CINNAMON_SCHEMA, _CINNAMON_LIST_KEY], check=False
            )
            for slug in _parse_gsettings_strv(raw_list):
                if slug.startswith("spawnkey-"):
                    found.add(slug if slug.endswith(".desktop") else f"{slug}.desktop")

        if self.use_system_tools and self.desktop_kind == "xfce" and shutil.which("xfconf-query"):
            out = self._run_tool(
                "xfconf-query",
                ["-c", _XFCE_CHANNEL, "-p", "/commands/custom", "-l", "-v"],
                check=False,
            )
            scripts_prefix = str(self.paths.scripts_dir)
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[1].strip().startswith(scripts_prefix):
                    script_stem = Path(parts[1].strip()).stem
                    if script_stem:
                        found.add(f"spawnkey-{script_stem}.desktop")

        return sorted(found)

    def find_system_conflicts(self, shortcut: str) -> list[str]:
        """Return non-SpawnKey system shortcuts colliding with the requested key combination."""
        delegate = self._get_delegate()
        if delegate is not None and hasattr(delegate, "find_system_conflicts"):
            return delegate.find_system_conflicts(shortcut)  # type: ignore[no-any-return]

        if not self.use_system_tools or not (shortcut or "").strip():
            return []
        try:
            parsed = parse_common_shortcut(shortcut)
        except ValueError:
            return []

        conflicts: list[str] = []
        scripts_prefix = str(self.paths.scripts_dir)

        if self.desktop_kind == "xfce" and shutil.which("xfconf-query"):
            xfce_accel = shortcut_to_x11_accel(parsed, style="xfce")
            for prefix in ("/commands/custom", "/xfwm4/custom"):
                out = self._run_tool(
                    "xfconf-query",
                    ["-c", _XFCE_CHANNEL, "-p", prefix, "-l", "-v"],
                    check=False,
                )
                for line in out.splitlines():
                    parts = line.split(None, 1)
                    if len(parts) != 2:
                        continue
                    prop, val = parts[0].strip(), parts[1].strip()
                    if prop.endswith(f"/{xfce_accel}") and not val.startswith(scripts_prefix):
                        conflicts.append(f"XFCE ({prop}): {val}")

        if self.desktop_kind == "mate" and shutil.which("gsettings"):
            gtk_accel = shortcut_to_x11_accel(parsed, style="gtk")
            for slot in range(1, _MATE_SLOT_COUNT + 1):
                bind = self._run_tool(
                    "gsettings", ["get", _MATE_GLOBAL_SCHEMA, f"run-command-{slot}"], check=False
                ).strip("'\"")
                cmd = self._run_tool(
                    "gsettings", ["get", _MATE_COMMANDS_SCHEMA, f"command-{slot}"], check=False
                ).strip("'\"")
                if bind == gtk_accel and not cmd.startswith(scripts_prefix):
                    conflicts.append(f"MATE (run-command-{slot}): {cmd or bind}")

        return conflicts

    def diagnose(self, profiles: list[DropdownProfile] | None = None) -> list[tuple[str, str, str]]:
        """Report X11 shortcut backend health and optional profile registration status."""
        delegate = self._get_delegate()
        if delegate is not None and hasattr(delegate, "diagnose"):
            return delegate.diagnose(profiles)  # type: ignore[no-any-return]

        results: list[tuple[str, str, str]] = [
            ("ok", "Escritorio X11", f"Modo detectado: {self.desktop_kind}"),
        ]

        if self.desktop_kind in ("cinnamon", "mate"):
            tool = "gsettings"
        elif self.desktop_kind == "xfce":
            tool = "xfconf-query"
        else:
            tool = "xbindkeys"

        tool_path = shutil.which(tool)
        results.append(
            (
                "ok" if tool_path else "warn",
                f"Herramienta ({tool})",
                tool_path or f"'{tool}' no está en PATH.",
            )
        )

        bindings = self._load_store()
        results.append(
            (
                "ok",
                "Registro X11",
                f"{len(bindings)} atajo(s) administrados en {self.store_path.name}.",
            )
        )

        if profiles:
            active = 0
            out_of_sync = 0
            for prof in profiles:
                status = self.get_shortcut_status(prof)
                if status is ShortcutRegistrationStatus.ACTIVE:
                    active += 1
                elif status is ShortcutRegistrationStatus.OUT_OF_SYNC:
                    out_of_sync += 1
            results.append(
                (
                    "warn" if out_of_sync else "ok",
                    "Perfiles",
                    f"{active} activos, {out_of_sync} desincronizados (de {len(profiles)}).",
                )
            )
        return results
