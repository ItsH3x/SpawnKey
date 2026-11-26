"""Sway global shortcut manager (spawnkey-binds.conf + bindsym/unbindsym IPC)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import tempfile

from spawnkey.backends.base import (
    BackendError,
    DiagnosticItem,
    ShortcutBackend,
    ShortcutRegistrationStatus,
)
from spawnkey.backends.sway.window_backend import _sway_cmd
from spawnkey.core.models import DropdownProfile
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import ParsedShortcut, parse_shortcut as parse_common_shortcut
from spawnkey.core.storage import AppPaths


_SWAY_MODIFIER_MAP: dict[str, str] = {
    "Meta": "Mod4",
    "Ctrl": "Control",
    "Alt": "Mod1",
    "Shift": "Shift",
}

_SWAY_KEY_MAP: dict[str, str] = {
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

_ID_LINE_RE = re.compile(r"^#\s*spawnkey-id:\s*(\S+)\s*$")
_BINDSYM_LINE_RE = re.compile(r"^bindsym\s+(?:--[a-z-]+\s+)*(\S+)\s+exec\s+(.+)$")


def shortcut_to_sway_bind(parsed: ParsedShortcut) -> str:
    """Translate a normalized ParsedShortcut into a Sway `bindsym` key combination.

    Example:
        Meta+Shift+T -> Mod4+Shift+t
        Ctrl+Alt+Space -> Control+Mod1+space
    """
    mods = [_SWAY_MODIFIER_MAP[m] for m in parsed.modifiers]
    if parsed.key in _SWAY_KEY_MAP:
        key_sym = _SWAY_KEY_MAP[parsed.key]
    elif len(parsed.key) == 1 and parsed.key.isalpha():
        key_sym = parsed.key.lower()
    else:
        key_sym = parsed.key
    return "+".join([*mods, key_sym])


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".conf", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class SwayShortcutBackend(ShortcutBackend):
    """Registers, audits, and cleanly removes SpawnKey global shortcuts on Sway."""

    name = "sway"

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        binds_conf_path: Path | None = None,
        main_conf_path: Path | None = None,
        use_ipc: bool = True,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        self.binds_conf_path = binds_conf_path or (config_home / "sway" / "spawnkey-binds.conf")
        self.main_conf_path = main_conf_path or (config_home / "sway" / "config")
        self.use_ipc = use_ipc

    def component_id(self, profile: DropdownProfile) -> str:
        return profile.shortcut_backend_id or f"spawnkey-{profile.id}.desktop"

    @staticmethod
    def _validate_component_id(component_id: str) -> None:
        if not component_id.startswith("spawnkey-"):
            raise ValueError(
                f"Seguridad: se rechazó operar sobre '{component_id}' porque no pertenece a SpawnKey."
            )

    def _read_entries(self) -> tuple[dict[str, tuple[str, str]], list[str]]:
        """Parse spawnkey-binds.conf into managed entries and non-managed preserved lines."""
        entries: dict[str, tuple[str, str]] = {}
        preserved: list[str] = []
        if not self.binds_conf_path.exists():
            return entries, preserved

        lines = self.binds_conf_path.read_text(encoding="utf-8", errors="replace").splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            id_match = _ID_LINE_RE.match(line.strip())
            if id_match:
                comp_id = id_match.group(1)
                if idx + 1 < len(lines):
                    bind_match = _BINDSYM_LINE_RE.match(lines[idx + 1].strip())
                    if bind_match:
                        combo = bind_match.group(1).strip()
                        raw_cmd = bind_match.group(2).strip()
                        try:
                            cmd_parts = shlex.split(raw_cmd)
                            cmd = cmd_parts[0] if len(cmd_parts) == 1 else raw_cmd
                        except ValueError:
                            cmd = raw_cmd
                        entries[comp_id] = (combo, cmd)
                        idx += 2
                        continue
            preserved.append(line)
            idx += 1

        return entries, preserved

    def _write_entries(
        self,
        entries: dict[str, tuple[str, str]],
        preserved: list[str],
    ) -> None:
        out_lines = [line for line in preserved if line.strip() != "# Managed by SpawnKey"]
        blocks: list[str] = ["# Managed by SpawnKey"]
        if out_lines:
            blocks.extend(out_lines)
        for comp_id in sorted(entries):
            combo, cmd = entries[comp_id]
            blocks.append(f"# spawnkey-id: {comp_id}")
            blocks.append(f"bindsym {combo} exec {shlex.quote(cmd)}")
        _atomic_write_text(self.binds_conf_path, "\n".join(blocks).rstrip("\n") + "\n")

    def _main_conf_includes_binds(self) -> bool:
        if not self.main_conf_path.exists():
            return False
        target_resolved = str(self.binds_conf_path.resolve())
        target_str = str(self.binds_conf_path)
        for raw_line in self.main_conf_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.match(r"^include\s+(.+)$", stripped)
            if not match:
                continue
            inc_val = match.group(1).strip().strip("'\"")
            expanded = os.path.expanduser(inc_val)
            if inc_val == target_str or expanded == target_str:
                return True
            try:
                if str(Path(expanded).resolve()) == target_resolved:
                    return True
            except OSError:
                pass
        return False

    def _ensure_included_in_main_conf(self) -> None:
        if not self.main_conf_path.exists() or self._main_conf_includes_binds():
            return
        existing = self.main_conf_path.read_text(encoding="utf-8", errors="replace")
        addition = f"\n# SpawnKey managed shortcuts\ninclude {self.binds_conf_path}\n"
        _atomic_write_text(self.main_conf_path, existing.rstrip("\n") + addition)

    def register_shortcut(self, profile: DropdownProfile) -> None:
        if not profile.shortcut or not profile.shortcut.strip():
            raise ValueError(f"El perfil '{profile.id}' no tiene una combinación de teclas asignada.")

        parsed = parse_common_shortcut(profile.shortcut)
        combo = shortcut_to_sway_bind(parsed)
        component = self.component_id(profile)
        self._validate_component_id(component)

        script_path = profile.script_path
        if not script_path or not Path(script_path).exists():
            script_path = str(ScriptGenerator(self.paths).generate(profile))

        entries, preserved = self._read_entries()
        old_entry = entries.get(component)
        entries[component] = (combo, script_path)
        self._write_entries(entries, preserved)
        self._ensure_included_in_main_conf()

        if self.use_ipc:
            if old_entry is not None:
                old_combo, _ = old_entry
                try:
                    _sway_cmd(f"unbindsym {old_combo}")
                except BackendError:
                    pass
            _sway_cmd(f"bindsym {combo} exec {shlex.quote(script_path)}")

        profile.shortcut = parsed.canonical
        profile.shortcut_enabled = True
        profile.shortcut_backend_id = component

    def remove_shortcut(self, profile: DropdownProfile) -> bool:
        component = self.component_id(profile)
        return self.remove_shortcut_by_component(component)

    def remove_shortcut_by_component(self, component_id: str) -> bool:
        self._validate_component_id(component_id)
        entries, preserved = self._read_entries()
        if component_id not in entries:
            return False

        old_combo, _ = entries.pop(component_id)
        self._write_entries(entries, preserved)

        if self.use_ipc:
            try:
                _sway_cmd(f"unbindsym {old_combo}")
            except BackendError:
                try:
                    _sway_cmd("reload")
                except BackendError:
                    pass

        return True

    def get_shortcut_status(self, profile: DropdownProfile) -> ShortcutRegistrationStatus:
        if not profile.shortcut or not profile.shortcut.strip():
            return ShortcutRegistrationStatus.UNASSIGNED
        if not profile.shortcut_enabled:
            return ShortcutRegistrationStatus.DISABLED

        try:
            expected = parse_common_shortcut(profile.shortcut)
            expected_combo = shortcut_to_sway_bind(expected)
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        component = self.component_id(profile)
        entries, _ = self._read_entries()
        entry = entries.get(component)
        if not entry:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        combo, cmd = entry
        if combo.lower() != expected_combo.lower():
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if profile.script_path and cmd != profile.script_path:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if self.main_conf_path.exists() and not self._main_conf_includes_binds():
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        return ShortcutRegistrationStatus.ACTIVE

    def list_managed_shortcut_ids(self) -> list[str]:
        entries, _ = self._read_entries()
        return sorted(comp for comp in entries if comp.startswith("spawnkey-"))

    def diagnose(self, profiles: list[DropdownProfile] | None = None) -> list[DiagnosticItem]:
        """Check Sway shortcut config inclusion."""
        items: list[DiagnosticItem] = []
        if self.binds_conf_path.exists():
            items.append(("ok", "Archivo de atajos Sway", f"Existe: {self.binds_conf_path}"))
        else:
            items.append(("warn", "Archivo de atajos Sway", f"Aún no creado: {self.binds_conf_path}"))

        if not self.main_conf_path.exists():
            items.append(("warn", "sway/config", f"No encontrado en {self.main_conf_path}"))
        elif self._main_conf_includes_binds():
            items.append(("ok", "sway/config", f"Incluye include {self.binds_conf_path}"))
        else:
            items.append(("warn", "sway/config", f"No incluye aún 'include {self.binds_conf_path}'"))

        _ = profiles
        return items
