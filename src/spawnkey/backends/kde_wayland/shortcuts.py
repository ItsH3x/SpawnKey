"""KDE Plasma 6 global shortcut manager (kglobalaccel + kglobalshortcutsrc)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

from spawnkey.backends.base import (
    DiagnosticItem,
    ShortcutBackend,
    ShortcutRegistrationStatus,
)
from spawnkey.backends.kde_wayland.dbus_utils import DBusError, find_busctl, run_cmd
from spawnkey.core.models import DropdownProfile
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import (
    ParsedShortcut,
    normalize_shortcut_key,
    parse_shortcut as parse_common_shortcut,
)
from spawnkey.core.storage import AppPaths
from spawnkey.i18n import _


# Qt6 keyboard modifier flags (qnamespace.h)
_QT_MODIFIER_FLAGS: dict[str, int] = {
    "Meta": 0x10000000,
    "Ctrl": 0x04000000,
    "Alt": 0x08000000,
    "Shift": 0x02000000,
}

# Common Qt6 Key codes mapped from canonical key labels (qnamespace.h)
_QT_KEY_CODES: dict[str, int] = {
    "Esc": 0x01000000,
    "Tab": 0x01000001,
    "Backspace": 0x01000003,
    "Return": 0x01000004,
    "Ins": 0x01000006,
    "Del": 0x01000007,
    "Home": 0x01000010,
    "End": 0x01000011,
    "Left": 0x01000012,
    "Up": 0x01000013,
    "Right": 0x01000014,
    "Down": 0x01000015,
    "PgUp": 0x01000016,
    "PgDown": 0x01000017,
    "Space": 0x20,
    "`": 0x60,
    "-": 0x2D,
    "=": 0x3D,
    "+": 0x2B,
    ",": 0x2C,
    ".": 0x2E,
    "/": 0x2F,
    "\\": 0x5C,
    ";": 0x3B,
    "'": 0x27,
    "[": 0x5B,
    "]": 0x5D,
    "Print": 0x01000009,
    "Pause": 0x01000008,
    "Menu": 0x01000055,
    "KP_Add": 0x20000000 | 0x2B,
    "KP_Subtract": 0x20000000 | 0x2D,
    "KP_Multiply": 0x20000000 | 0x2A,
    "KP_Divide": 0x20000000 | 0x2F,
    "KP_Decimal": 0x20000000 | 0x2E,
}

for _n in range(1, 25):
    _QT_KEY_CODES[f"F{_n}"] = 0x01000030 + (_n - 1)

for _d in "0123456789":
    _QT_KEY_CODES[f"KP_{_d}"] = 0x20000000 | ord(_d)


def shortcut_to_qt_key(parsed: ParsedShortcut) -> int:
    """Translate a normalized ParsedShortcut into a Qt6 combined key/modifier integer."""
    mod_mask = 0
    for mod in parsed.modifiers:
        mod_mask |= _QT_MODIFIER_FLAGS[mod]

    if parsed.key in _QT_KEY_CODES:
        key_code = _QT_KEY_CODES[parsed.key]
    elif len(parsed.key) == 1:
        key_code = ord(parsed.key)
    else:
        raise ValueError(f"Tecla sin código Qt conocido: '{parsed.key}'.")

    return mod_mask | key_code


def parse_shortcut(shortcut: str) -> tuple[str, int]:
    """Normalize a shortcut string and return (canonical_label, qt_key_int)."""
    parsed = parse_common_shortcut(shortcut)
    return parsed.canonical, shortcut_to_qt_key(parsed)


def _find_kwriteconfig() -> str:
    for candidate in ("kwriteconfig6", "kwriteconfig5"):
        found = shutil.which(candidate)
        if found:
            return found
    raise DBusError("No se encontró 'kwriteconfig6' en PATH.")


def _find_kreadconfig() -> str | None:
    for candidate in ("kreadconfig6", "kreadconfig5"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


class KDEWaylandShortcutBackend(ShortcutBackend):
    """Registers, audits, and cleanly removes SpawnKey global shortcuts in KDE Plasma 6."""

    name = "kde-wayland"
    ACTION_UNIQUE = "_launch"

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        applications_dir: Path | None = None,
        kglobalshortcutsrc_path: Path | None = None,
        use_dbus: bool = True,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        self.applications_dir = applications_dir or (data_home / "applications")
        self.kglobalshortcutsrc_path = kglobalshortcutsrc_path or (config_home / "kglobalshortcutsrc")
        self.use_dbus = use_dbus

    def component_id(self, profile: DropdownProfile) -> str:
        return profile.shortcut_backend_id or f"spawnkey-{profile.id}.desktop"

    def desktop_file_path(self, component_id: str) -> Path:
        return self.applications_dir / component_id

    def _write_service_desktop(self, profile: DropdownProfile, component: str) -> Path:
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
            "X-KDE-StartupNotify=false\n"
            "X-KDE-GlobalAccel-CommandShortcut=true\n"
            f"X-SpawnKey-Profile={profile.id}\n"
        )
        target.write_text(content, encoding="utf-8")
        return target

    def _dbus_action_id(self, profile: DropdownProfile, component: str) -> list[str]:
        return [component, self.ACTION_UNIQUE, f"SpawnKey: {profile.name}", f"Alternar {profile.name}"]

    def _component_dbus_path(self, component: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]", "_", component)
        return f"/component/{safe}"

    def _dbus_register(self, profile: DropdownProfile, component: str, qt_key: int) -> None:
        if not self.use_dbus:
            return
        busctl = find_busctl()
        action_id = self._dbus_action_id(profile, component)
        # Register component/action in kglobalacceld
        run_cmd(
            [
                busctl,
                "--user",
                "call",
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                "doRegister",
                "as",
                str(len(action_id)),
                *action_id,
            ],
            check=True,
        )
        # Set shortcut with SetPresent (0x2) | NoAutoloading (0x4) = 6 so kglobalacceld
        # immediately activates and grabs the key without needing a relogin.
        run_cmd(
            [
                busctl,
                "--user",
                "call",
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                "setShortcutKeys",
                "asa(ai)u",
                str(len(action_id)),
                *action_id,
                "1",
                "1",
                str(qt_key),
                "6",
            ],
            check=True,
        )
        # Reinforce with setForeignShortcutKeys so KDE persists the binding immediately.
        run_cmd(
            [
                busctl,
                "--user",
                "call",
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                "setForeignShortcutKeys",
                "asa(ai)",
                str(len(action_id)),
                *action_id,
                "1",
                "1",
                str(qt_key),
            ],
            check=False,
        )

    def _dbus_unregister(self, component: str, friendly_name: str = "SpawnKey") -> None:
        if not self.use_dbus:
            return
        busctl = find_busctl()
        action_id = [component, self.ACTION_UNIQUE, friendly_name, "Launch"]
        # Release active key grab first
        run_cmd(
            [
                busctl,
                "--user",
                "call",
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                "setForeignShortcutKeys",
                "asa(ai)",
                str(len(action_id)),
                *action_id,
                "0",
            ],
            check=False,
        )
        # Unregister from kglobalacceld
        run_cmd(
            [
                busctl,
                "--user",
                "call",
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                "unregister",
                "ss",
                component,
                self.ACTION_UNIQUE,
            ],
            check=False,
        )
        # Clean up the D-Bus component object so it disappears from System Settings
        run_cmd(
            [
                busctl,
                "--user",
                "call",
                "org.kde.kglobalaccel",
                self._component_dbus_path(component),
                "org.kde.kglobalaccel.Component",
                "cleanUp",
            ],
            check=False,
        )

    def _read_config_shortcut(self, component: str) -> str:
        if self.kglobalshortcutsrc_path.exists():
            text = self.kglobalshortcutsrc_path.read_text(encoding="utf-8", errors="replace")
            header = f"[services][{component}]"
            in_section = False
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_section = line == header
                    continue
                if in_section and line.startswith(f"{self.ACTION_UNIQUE}="):
                    val = line.split("=", 1)[1].strip()
                    # kglobalshortcutsrc can store "Meta+Shift+T,none,Launch" or "Meta+Shift+T"
                    primary = val.split(",", 1)[0].strip()
                    return "" if primary.lower() == "none" else primary
        return ""

    def _remove_config_section(self, component: str) -> bool:
        if not self.kglobalshortcutsrc_path.exists():
            return False
        text = self.kglobalshortcutsrc_path.read_text(encoding="utf-8", errors="replace")
        header = f"[services][{component}]"
        lines = text.splitlines()
        out: list[str] = []
        skipping = False
        removed = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if stripped == header:
                    skipping = True
                    removed = True
                    continue
                skipping = False
            if not skipping:
                out.append(line)

        if removed:
            cleaned = "\n".join(out).rstrip("\n") + "\n"
            self.kglobalshortcutsrc_path.write_text(cleaned, encoding="utf-8")
        return removed

    def register_shortcut(self, profile: DropdownProfile) -> None:
        if not profile.shortcut or not profile.shortcut.strip():
            raise ValueError(f"El perfil '{profile.id}' no tiene una combinación de teclas asignada.")

        canonical, qt_key = parse_shortcut(profile.shortcut)
        component = self.component_id(profile)

        self._write_service_desktop(profile, component)

        if self.use_dbus:
            kwrite = _find_kwriteconfig()
            run_cmd(
                [
                    kwrite,
                    "--file",
                    str(self.kglobalshortcutsrc_path),
                    "--group",
                    "services",
                    "--group",
                    component,
                    "--key",
                    self.ACTION_UNIQUE,
                    canonical,
                ],
                check=False,
            )
            # Refresh KDE service cache if kbuildsycoca6 is present
            if shutil.which("kbuildsycoca6"):
                run_cmd(["kbuildsycoca6"], check=False)
            self._dbus_register(profile, component, qt_key)
        else:
            # Isolated file mode for unit tests
            self._remove_config_section(component)
            self.kglobalshortcutsrc_path.parent.mkdir(parents=True, exist_ok=True)
            existing = (
                self.kglobalshortcutsrc_path.read_text(encoding="utf-8")
                if self.kglobalshortcutsrc_path.exists()
                else ""
            )
            block = f"\n[services][{component}]\n{self.ACTION_UNIQUE}={canonical}\n"
            self.kglobalshortcutsrc_path.write_text(existing.rstrip() + block, encoding="utf-8")

        profile.shortcut = canonical
        profile.shortcut_enabled = True
        profile.shortcut_backend_id = component

    def remove_shortcut(self, profile: DropdownProfile) -> bool:
        component = self.component_id(profile)
        return self.remove_shortcut_by_component(component, friendly_name=f"SpawnKey: {profile.name}")

    def remove_shortcut_by_component(self, component_id: str, friendly_name: str = "SpawnKey") -> bool:
        """Remove a SpawnKey-managed shortcut entry cleanly by its component ID."""
        if not component_id.startswith("spawnkey-"):
            raise ValueError(f"Seguridad: se rechazó remover '{component_id}' porque no pertenece a SpawnKey.")

        removed_any = False
        self._dbus_unregister(component_id, friendly_name=friendly_name)

        desktop_file = self.desktop_file_path(component_id)
        if desktop_file.exists():
            desktop_file.unlink()
            removed_any = True

        if self.use_dbus and shutil.which("kwriteconfig6"):
            run_cmd(
                [
                    "kwriteconfig6",
                    "--file",
                    str(self.kglobalshortcutsrc_path),
                    "--group",
                    "services",
                    "--group",
                    component_id,
                    "--key",
                    self.ACTION_UNIQUE,
                    "--delete",
                ],
                check=False,
            )
        if self._remove_config_section(component_id):
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
            expected, _ = parse_shortcut(profile.shortcut)
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        configured = self._read_config_shortcut(component)
        if not configured:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        try:
            configured_canon, _ = parse_shortcut(configured)
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        if configured_canon != expected:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        # Verify Exec in .desktop points to current script_path
        if profile.script_path:
            content = desktop_file.read_text(encoding="utf-8", errors="replace")
            if f"Exec={profile.script_path}" not in content:
                return ShortcutRegistrationStatus.OUT_OF_SYNC

        return ShortcutRegistrationStatus.ACTIVE

    def list_managed_shortcut_ids(self) -> list[str]:
        found: set[str] = set()
        if self.applications_dir.is_dir():
            for entry in self.applications_dir.glob("spawnkey-*.desktop"):
                found.add(entry.name)
        if self.kglobalshortcutsrc_path.exists():
            text = self.kglobalshortcutsrc_path.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"^\[services\]\[(spawnkey-[^\]]+\.desktop)\]$", text, re.MULTILINE):
                found.add(match.group(1))
        return sorted(found)

    def find_system_conflicts(self, shortcut: str) -> list[str]:
        """Scan non-SpawnKey KDE shortcut sections in kglobalshortcutsrc for collisions."""
        target = normalize_shortcut_key(shortcut)
        if not target or not self.kglobalshortcutsrc_path.exists():
            return []

        try:
            text = self.kglobalshortcutsrc_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        conflicts: list[str] = []
        current_section = ""
        in_spawnkey_section = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line
                in_spawnkey_section = line.startswith("[services][spawnkey-")
                continue
            if in_spawnkey_section or not current_section or "=" not in line:
                continue
            if current_section in ("[$Version]", "[General]"):
                continue

            action_name, val = line.split("=", 1)
            action_name = action_name.strip()
            if not action_name or action_name == "_k_friendly_name":
                continue

            parts = val.split(",")
            raw_primary = parts[0].strip()
            if not raw_primary or raw_primary.lower() == "none":
                continue

            for binding in raw_primary.split("\t"):
                cand = binding.strip()
                if not cand or cand.lower() == "none":
                    continue
                if normalize_shortcut_key(cand) == target:
                    label = parts[2].strip() if len(parts) > 2 and parts[2].strip() else action_name
                    sec_label = current_section.strip("[]").replace("][", " / ")
                    desc = f"{sec_label}: {label}"
                    if desc not in conflicts:
                        conflicts.append(desc)
                    break

        return conflicts

    def diagnose(self, profiles: list[DropdownProfile] | None = None) -> list[DiagnosticItem]:
        """Verify KDE shortcut CLI/D-Bus tools and check for system shortcut conflicts."""
        items: list[DiagnosticItem] = []

        kwrite = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
        if kwrite:
            items.append(("ok", "kwriteconfig", _("Available: {path}").format(path=kwrite)))
        else:
            items.append(
                (
                    "error",
                    "kwriteconfig",
                    _("Neither 'kwriteconfig6' nor 'kwriteconfig5' was found in PATH."),
                )
            )

        busctl = shutil.which("busctl")
        if busctl:
            items.append(("ok", "busctl", _("Available: {path}").format(path=busctl)))
        else:
            items.append(("error", "busctl", _("'busctl' was not found in PATH.")))

        if not self.use_dbus:
            items.append(("ok", "kglobalaccel", _("Isolated file mode (D-Bus disabled).")))
        elif not busctl:
            items.append(("warn", "kglobalaccel", _("Cannot verify because 'busctl' is missing.")))
        else:
            try:
                run_cmd(
                    [
                        busctl,
                        "--user",
                        "call",
                        "org.kde.kglobalaccel",
                        "/kglobalaccel",
                        "org.kde.KGlobalAccel",
                        "allComponents",
                    ],
                    check=True,
                    timeout=3.0,
                )
                items.append(
                    (
                        "ok",
                        "kglobalaccel",
                        _("Service org.kde.kglobalaccel is active on D-Bus."),
                    )
                )
            except Exception as exc:
                items.append(
                    (
                        "error",
                        "kglobalaccel",
                        _("Service org.kde.kglobalaccel is not responding: {error}").format(
                            error=exc
                        ),
                    )
                )

        if profiles:
            had_conflict = False
            for profile in profiles:
                if not profile.shortcut or not profile.shortcut.strip() or not profile.shortcut_enabled:
                    continue
                conflicts = self.find_system_conflicts(profile.shortcut)
                if conflicts:
                    had_conflict = True
                    items.append(
                        (
                            "warn",
                            _("Shortcut conflict ({profile_id})").format(profile_id=profile.id),
                            _("'{shortcut}' conflicts with: {conflicts}").format(
                                shortcut=profile.shortcut,
                                conflicts=", ".join(conflicts),
                            ),
                        )
                    )
            if not had_conflict:
                items.append(
                    (
                        "ok",
                        _("Shortcut conflicts"),
                        _("No collisions with system shortcuts."),
                    )
                )

        return items
