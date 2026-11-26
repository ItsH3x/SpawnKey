"""Hyprland global shortcut manager (spawnkey-binds.conf + keyword bind/unbind IPC)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from spawnkey.backends.base import (
    BackendError,
    DiagnosticItem,
    ShortcutBackend,
    ShortcutRegistrationStatus,
)
from spawnkey.backends.hyprland.window_backend import _hypr_ipc, _hypr_json
from spawnkey.core.models import DropdownProfile
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import ParsedShortcut, parse_shortcut as parse_common_shortcut
from spawnkey.core.storage import AppPaths


_HYPRLAND_MODIFIER_MAP: dict[str, str] = {
    "Meta": "SUPER",
    "Ctrl": "CTRL",
    "Alt": "ALT",
    "Shift": "SHIFT",
}

_HYPRLAND_KEY_MAP: dict[str, str] = {
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
    **{f"KP_{i}": f"KP_{i}" for i in range(10)},
}

# Hyprland / xkb modifier bitmask values returned by `hyprctl -j binds`
_HYPR_MODMASK_BITS: dict[str, int] = {
    "SHIFT": 1,
    "CTRL": 4,
    "ALT": 8,
    "SUPER": 64,
}

_HYPR_MOD_ALIASES: dict[str, str] = {
    "SUPER": "SUPER",
    "MOD4": "SUPER",
    "META": "SUPER",
    "LOGO": "SUPER",
    "WIN": "SUPER",
    "CTRL": "CTRL",
    "CONTROL": "CTRL",
    "ALT": "ALT",
    "MOD1": "ALT",
    "SHIFT": "SHIFT",
}

_ID_LINE_RE = re.compile(r"^#\s*spawnkey-id:\s*(\S+)\s*$")
_BIND_LINE_RE = re.compile(r"^bind[a-z]*\s*=\s*([^,]*),\s*([^,]+),\s*([^,]+)(?:,\s*(.*))?$")


def shortcut_to_hyprland_bind(parsed: ParsedShortcut) -> tuple[str, str]:
    """Translate a normalized ParsedShortcut into Hyprland (modifiers, key) pair.

    Example:
        Meta+Shift+T -> ("SUPER SHIFT", "T")
    """
    mods = " ".join(_HYPRLAND_MODIFIER_MAP[m] for m in parsed.modifiers)
    if parsed.key in _HYPRLAND_KEY_MAP:
        key_sym = _HYPRLAND_KEY_MAP[parsed.key]
    else:
        key_sym = parsed.key
    return mods, key_sym


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


def _normalize_hypr_mods(mods_raw: str) -> tuple[str, ...]:
    tokens: set[str] = set()
    for part in re.split(r"[\s_+]+", mods_raw.strip()):
        if not part:
            continue
        norm = _HYPR_MOD_ALIASES.get(part.upper())
        if norm:
            tokens.add(norm)
    order = ("SUPER", "CTRL", "ALT", "SHIFT")
    return tuple(m for m in order if m in tokens)


class HyprlandShortcutBackend(ShortcutBackend):
    """Registers, audits, and cleanly removes SpawnKey global shortcuts on Hyprland."""

    name = "hyprland"

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
        self.binds_conf_path = binds_conf_path or (config_home / "hypr" / "spawnkey-binds.conf")
        self.main_conf_path = main_conf_path or (config_home / "hypr" / "hyprland.conf")
        self.use_ipc = use_ipc

    def component_id(self, profile: DropdownProfile) -> str:
        return profile.shortcut_backend_id or f"spawnkey-{profile.id}.desktop"

    @staticmethod
    def _validate_component_id(component_id: str) -> None:
        if not component_id.startswith("spawnkey-"):
            raise ValueError(
                f"Seguridad: se rechazó operar sobre '{component_id}' porque no pertenece a SpawnKey."
            )

    def _read_entries(self) -> tuple[dict[str, tuple[str, str, str]], list[str]]:
        """Parse spawnkey-binds.conf into managed entries and non-managed preserved lines."""
        entries: dict[str, tuple[str, str, str]] = {}
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
                    bind_match = _BIND_LINE_RE.match(lines[idx + 1].strip())
                    if bind_match:
                        mods = bind_match.group(1).strip()
                        key = bind_match.group(2).strip()
                        dispatcher = bind_match.group(3).strip()
                        arg = (bind_match.group(4) or "").strip()
                        if dispatcher == "exec":
                            entries[comp_id] = (mods, key, arg)
                        idx += 2
                        continue
            preserved.append(line)
            idx += 1

        return entries, preserved

    def _write_entries(
        self,
        entries: dict[str, tuple[str, str, str]],
        preserved: list[str],
    ) -> None:
        out_lines = [line for line in preserved if line.strip() != "# Managed by SpawnKey"]
        header = "# Managed by SpawnKey"
        blocks: list[str] = [header]
        if out_lines:
            blocks.extend(out_lines)
        for comp_id in sorted(entries):
            mods, key, cmd = entries[comp_id]
            blocks.append(f"# spawnkey-id: {comp_id}")
            blocks.append(f"bind = {mods}, {key}, exec, {cmd}")
        _atomic_write_text(self.binds_conf_path, "\n".join(blocks).rstrip("\n") + "\n")

    def _main_conf_sources_binds(self) -> bool:
        if not self.main_conf_path.exists():
            return False
        target_resolved = str(self.binds_conf_path.resolve())
        target_str = str(self.binds_conf_path)
        for raw_line in self.main_conf_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.match(r"^source\s*=\s*(.+)$", stripped)
            if not match:
                continue
            src_val = match.group(1).strip().strip("'\"")
            expanded = os.path.expanduser(src_val)
            if src_val == target_str or expanded == target_str:
                return True
            try:
                if str(Path(expanded).resolve()) == target_resolved:
                    return True
            except OSError:
                pass
        return False

    def _ensure_sourced_in_main_conf(self) -> None:
        if not self.main_conf_path.exists() or self._main_conf_sources_binds():
            return
        existing = self.main_conf_path.read_text(encoding="utf-8", errors="replace")
        addition = f"\n# SpawnKey managed shortcuts\nsource = {self.binds_conf_path}\n"
        _atomic_write_text(self.main_conf_path, existing.rstrip("\n") + addition)

    def register_shortcut(self, profile: DropdownProfile) -> None:
        if not profile.shortcut or not profile.shortcut.strip():
            raise ValueError(f"El perfil '{profile.id}' no tiene una combinación de teclas asignada.")

        parsed = parse_common_shortcut(profile.shortcut)
        mods, key = shortcut_to_hyprland_bind(parsed)
        component = self.component_id(profile)
        self._validate_component_id(component)

        script_path = profile.script_path
        if not script_path or not Path(script_path).exists():
            script_path = str(ScriptGenerator(self.paths).generate(profile))

        entries, preserved = self._read_entries()
        old_entry = entries.get(component)
        entries[component] = (mods, key, script_path)
        self._write_entries(entries, preserved)
        self._ensure_sourced_in_main_conf()

        if self.use_ipc:
            if old_entry is not None:
                old_mods, old_key, _ = old_entry
                try:
                    _hypr_ipc(f"keyword unbind {old_mods}, {old_key}")
                except BackendError:
                    pass
            _hypr_ipc(f"keyword bind {mods}, {key}, exec, {script_path}")

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

        old_mods, old_key, _ = entries.pop(component_id)
        self._write_entries(entries, preserved)

        if self.use_ipc:
            try:
                _hypr_ipc(f"keyword unbind {old_mods}, {old_key}")
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
            expected_mods, expected_key = shortcut_to_hyprland_bind(expected)
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        component = self.component_id(profile)
        entries, _ = self._read_entries()
        entry = entries.get(component)
        if not entry:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        mods, key, cmd = entry
        if mods != expected_mods or key.lower() != expected_key.lower():
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if profile.script_path and cmd != profile.script_path:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if self.main_conf_path.exists() and not self._main_conf_sources_binds():
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        return ShortcutRegistrationStatus.ACTIVE

    def list_managed_shortcut_ids(self) -> list[str]:
        entries, _ = self._read_entries()
        return sorted(comp for comp in entries if comp.startswith("spawnkey-"))

    def find_system_conflicts(self, shortcut: str) -> list[str]:
        """Check whether a shortcut conflicts with non-SpawnKey Hyprland binds or hyprland.conf."""
        if not shortcut or not shortcut.strip():
            return []
        try:
            parsed = parse_common_shortcut(shortcut)
        except ValueError:
            return []
        target_mods, target_key = shortcut_to_hyprland_bind(parsed)
        norm_target_mods = _normalize_hypr_mods(target_mods)
        target_mask = sum(_HYPR_MODMASK_BITS[m] for m in norm_target_mods)
        target_key_low = target_key.lower()

        entries, _ = self._read_entries()
        managed_cmds = {cmd for _, _, cmd in entries.values()}
        scripts_prefix = str(self.paths.scripts_dir)

        def _is_own_bind(disp: str, arg: str) -> bool:
            return disp == "exec" and (arg in managed_cmds or arg.startswith(scripts_prefix))

        conflicts: list[str] = []

        if self.use_ipc:
            try:
                raw_binds = _hypr_json("binds")
                if isinstance(raw_binds, list):
                    for bind in raw_binds:
                        if not isinstance(bind, dict):
                            continue
                        b_mask = int(bind.get("modmask") or 0)
                        b_key = str(bind.get("key") or "").strip().lower()
                        if b_mask == target_mask and b_key == target_key_low:
                            disp = str(bind.get("dispatcher") or "")
                            arg = str(bind.get("arg") or "")
                            if _is_own_bind(disp, arg):
                                continue
                            desc = f"hyprland: {target_mods}, {target_key} -> {disp} {arg}".strip()
                            if desc not in conflicts:
                                conflicts.append(desc)
                    return conflicts
            except BackendError:
                pass

        if self.main_conf_path.exists():
            text = self.main_conf_path.read_text(encoding="utf-8", errors="replace")
            variables: dict[str, str] = {}
            for raw_line in text.splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                var_match = re.match(r"^(\$[A-Za-z0-9_]+)\s*=\s*(.+)$", stripped)
                if var_match:
                    variables[var_match.group(1)] = var_match.group(2).strip()
                    continue
                bind_match = _BIND_LINE_RE.match(stripped)
                if not bind_match:
                    continue
                mods_expr = bind_match.group(1).strip()
                for var_name, var_val in variables.items():
                    mods_expr = mods_expr.replace(var_name, var_val)
                b_mods = _normalize_hypr_mods(mods_expr)
                b_key = bind_match.group(2).strip().lower()
                if b_mods == norm_target_mods and b_key == target_key_low:
                    disp = bind_match.group(3).strip()
                    arg = (bind_match.group(4) or "").strip()
                    if _is_own_bind(disp, arg):
                        continue
                    conflicts.append(
                        f"hyprland.conf: {mods_expr}, {bind_match.group(2).strip()} -> {disp} {arg}".strip()
                    )

        return conflicts

    def diagnose(self, profiles: list[DropdownProfile] | None = None) -> list[DiagnosticItem]:
        """Check Hyprland shortcut config sourcing and optional profile shortcut conflicts."""
        items: list[DiagnosticItem] = []
        if self.binds_conf_path.exists():
            items.append(("ok", "Archivo de atajos Hyprland", f"Existe: {self.binds_conf_path}"))
        else:
            items.append(("warn", "Archivo de atajos Hyprland", f"Aún no creado: {self.binds_conf_path}"))

        if not self.main_conf_path.exists():
            items.append(("warn", "hyprland.conf", f"No encontrado en {self.main_conf_path}"))
        elif self._main_conf_sources_binds():
            items.append(("ok", "hyprland.conf", f"Incluye source = {self.binds_conf_path}"))
        else:
            items.append(("warn", "hyprland.conf", f"No incluye aún 'source = {self.binds_conf_path}'"))

        for prof in profiles or ():
            if prof.shortcut and prof.shortcut_enabled:
                for conflict in self.find_system_conflicts(prof.shortcut):
                    items.append(("warn", f"Conflicto ({prof.id})", conflict))
        return items
