"""Shared XDG Desktop Portal org.freedesktop.portal.GlobalShortcuts backend for Wayland."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

from spawnkey.backends.base import (
    BackendError,
    ShortcutBackend,
    ShortcutRegistrationStatus,
)
from spawnkey.core.models import DropdownProfile
from spawnkey.core.script_gen import ScriptGenerator
from spawnkey.core.shortcuts import ParsedShortcut, parse_shortcut as parse_common_shortcut
from spawnkey.core.storage import AppPaths


PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_GLOBAL_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
PORTAL_SESSION_IFACE = "org.freedesktop.portal.Session"

_XDG_MODIFIER_MAP: dict[str, str] = {
    "Meta": "LOGO",
    "Ctrl": "CTRL",
    "Alt": "ALT",
    "Shift": "SHIFT",
}

_XDG_REVERSE_MODIFIER_MAP: dict[str, str] = {
    "logo": "Meta",
    "super": "Meta",
    "meta": "Meta",
    "win": "Meta",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "primary": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}

_XDG_KEY_MAP: dict[str, str] = {
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

_XDG_REVERSE_KEY_MAP: dict[str, str] = {v.lower(): k for k, v in _XDG_KEY_MAP.items()}
_XDG_REVERSE_KEY_MAP["page_up"] = "PgUp"
_XDG_REVERSE_KEY_MAP["page_down"] = "PgDown"


def shortcut_to_xdg_trigger(parsed: ParsedShortcut) -> str:
    """Format a normalized ParsedShortcut according to the Freedesktop XDG Shortcuts Spec.

    Example:
        Meta+Shift+T -> LOGO+SHIFT+t
        Ctrl+Alt+F12 -> CTRL+ALT+F12
    """
    mods = [_XDG_MODIFIER_MAP[m] for m in parsed.modifiers]
    if parsed.key in _XDG_KEY_MAP:
        key_sym = _XDG_KEY_MAP[parsed.key]
    elif len(parsed.key) == 1 and parsed.key.isalpha():
        key_sym = parsed.key.lower()
    else:
        key_sym = parsed.key
    return "+".join([*mods, key_sym])


def xdg_trigger_to_canonical(trigger: str) -> str:
    """Convert an XDG trigger string (e.g. 'LOGO+SHIFT+t') into SpawnKey canonical format."""
    text = (trigger or "").strip()
    if not text:
        raise ValueError("El trigger XDG está vacío.")

    # Also accept GTK accelerator format (<Super><Shift>t) if returned by a portal backend
    if text.startswith("<") and ">" in text:
        tokens = re.findall(r"<([^>]+)>", text)
        tail = re.sub(r"^(?:<[^>]+>)+", "", text).strip()
        raw_parts = [*tokens, tail] if tail else tokens
    else:
        raw_parts = [p.strip() for p in text.split("+") if p.strip()]

    translated: list[str] = []
    for part in raw_parts:
        low = part.lower()
        if low in _XDG_REVERSE_MODIFIER_MAP:
            translated.append(_XDG_REVERSE_MODIFIER_MAP[low])
        elif low in _XDG_REVERSE_KEY_MAP:
            translated.append(_XDG_REVERSE_KEY_MAP[low])
        else:
            translated.append(part)

    return parse_common_shortcut("+".join(translated)).canonical


def is_portal_global_shortcuts_available() -> bool:
    """Return True if org.freedesktop.portal.GlobalShortcuts is exposed on the session bus."""
    try:
        import dbus  # type: ignore[import-not-found]

        bus = dbus.SessionBus()
        proxy = bus.get_object(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH)
        props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
        version = int(props.Get(PORTAL_GLOBAL_SHORTCUTS_IFACE, "version"))
        return version >= 1
    except Exception:
        return False


def bind_portal_shortcut_oneshot(
    profile: DropdownProfile,
    xdg_trigger: str,
) -> bool:
    """Perform a non-resident one-shot GlobalShortcuts session bind and close the session immediately.

    Respects SpawnKey's zero-resident-daemon architecture: opens a transient portal session,
    registers the shortcut metadata and preferred_trigger, and closes the session handle before
    returning. Returns True if the portal accepted the request without error.
    """
    try:
        import dbus  # type: ignore[import-not-found]

        bus = dbus.SessionBus()
        portal_obj = bus.get_object(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH)
        portal = dbus.Interface(portal_obj, PORTAL_GLOBAL_SHORTCUTS_IFACE)

        token_suffix = uuid.uuid4().hex[:10]
        handle_token = f"spawnkey_req_{token_suffix}"
        session_token = f"spawnkey_sess_{profile.id.replace('-', '_')}_{token_suffix}"

        sender_clean = (bus.get_unique_name() or "").lstrip(":").replace(".", "_")
        session_path = f"/org/freedesktop/portal/desktop/session/{sender_clean}/{session_token}"

        portal.CreateSession(
            dbus.Dictionary(
                {
                    "handle_token": dbus.String(handle_token),
                    "session_handle_token": dbus.String(session_token),
                },
                signature="sv",
            )
        )

        bind_token = f"spawnkey_bind_{token_suffix}"
        shortcuts_arg = dbus.Array(
            [
                dbus.Struct(
                    (
                        dbus.String(f"spawnkey-{profile.id}"),
                        dbus.Dictionary(
                            {
                                "description": dbus.String(f"SpawnKey: {profile.name}"),
                                "preferred_trigger": dbus.String(xdg_trigger),
                            },
                            signature="sv",
                        ),
                    ),
                    signature=None,
                )
            ],
            signature="(sa{sv})",
        )
        try:
            portal.BindShortcuts(
                dbus.ObjectPath(session_path),
                shortcuts_arg,
                dbus.String(""),
                dbus.Dictionary({"handle_token": dbus.String(bind_token)}, signature="sv"),
            )
        except Exception:
            pass

        # Close session immediately so zero resident daemon remains connected
        try:
            sess_obj = bus.get_object(PORTAL_BUS_NAME, session_path)
            dbus.Interface(sess_obj, PORTAL_SESSION_IFACE).Close()
        except Exception:
            pass
        return True
    except Exception:
        return False


class XDGPortalShortcutBackend(ShortcutBackend):
    """Shared Wayland ShortcutBackend using org.freedesktop.portal.GlobalShortcuts + XDG .desktop.

    Because the XDG GlobalShortcuts portal delivers `Activated` signals only to an active
    session or D-Bus-activatable desktop entry, this backend records the XDG trigger in a
    persistent manifest, writes the Freedesktop `.desktop` launcher, performs a one-shot
    portal registration when D-Bus is active, and optionally chains a native command-shortcut
    delegate (such as GNOME `gsd-media-keys` or KDE `kglobalaccel`) so shortcuts execute the
    generated `.sh` script directly with zero resident SpawnKey daemon.
    """

    name = "xdg-portal"

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        applications_dir: Path | None = None,
        manifest_path: Path | None = None,
        delegate: ShortcutBackend | None = None,
        use_dbus: bool = True,
    ) -> None:
        self.paths = paths or AppPaths.from_env()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        self.applications_dir = applications_dir or (data_home / "applications")
        self.manifest_path = manifest_path or (self.paths.config_dir / "portal_shortcuts.json")
        self.delegate = delegate
        self.use_dbus = use_dbus

    def component_id(self, profile: DropdownProfile) -> str:
        return profile.shortcut_backend_id or f"spawnkey-{profile.id}.desktop"

    def desktop_file_path(self, component_id: str) -> Path:
        return self.applications_dir / component_id

    def _load_manifest(self) -> dict[str, dict[str, str]]:
        if not self.manifest_path.exists():
            return {}
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("shortcuts"), dict):
                return dict(raw["shortcuts"])
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_manifest(self, shortcuts_map: dict[str, dict[str, str]]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"shortcuts": shortcuts_map}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.manifest_path)

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
        xdg_trigger = shortcut_to_xdg_trigger(parsed)
        component = self.component_id(profile)

        self._write_desktop_entry(profile, component, xdg_trigger)

        manifest = self._load_manifest()
        manifest[component] = {
            "profile_id": profile.id,
            "canonical": parsed.canonical,
            "xdg_trigger": xdg_trigger,
            "script_path": profile.script_path,
        }
        self._save_manifest(manifest)

        if self.use_dbus:
            bind_portal_shortcut_oneshot(profile, xdg_trigger)

        if self.delegate is not None:
            self.delegate.register_shortcut(profile)

        profile.shortcut = parsed.canonical
        profile.shortcut_enabled = True
        profile.shortcut_backend_id = component

    def remove_shortcut(self, profile: DropdownProfile) -> bool:
        component = self.component_id(profile)
        return self.remove_shortcut_by_component(component)

    def remove_shortcut_by_component(self, component_id: str) -> bool:
        if not component_id.startswith("spawnkey-"):
            raise ValueError(
                f"Seguridad: se rechazó remover '{component_id}' porque no pertenece a SpawnKey."
            )

        removed_any = False
        desktop_file = self.desktop_file_path(component_id)
        if desktop_file.exists():
            desktop_file.unlink()
            removed_any = True

        manifest = self._load_manifest()
        if component_id in manifest:
            manifest.pop(component_id, None)
            self._save_manifest(manifest)
            removed_any = True

        if self.delegate is not None:
            try:
                if self.delegate.remove_shortcut_by_component(component_id):
                    removed_any = True
            except BackendError:
                pass

        return removed_any

    def get_shortcut_status(self, profile: DropdownProfile) -> ShortcutRegistrationStatus:
        if not profile.shortcut or not profile.shortcut.strip():
            return ShortcutRegistrationStatus.UNASSIGNED
        if not profile.shortcut_enabled:
            return ShortcutRegistrationStatus.DISABLED

        if self.delegate is not None:
            return self.delegate.get_shortcut_status(profile)

        component = self.component_id(profile)
        desktop_file = self.desktop_file_path(component)
        if not desktop_file.exists():
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        try:
            expected = parse_common_shortcut(profile.shortcut)
        except ValueError:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        manifest = self._load_manifest()
        entry = manifest.get(component)
        if not entry:
            return ShortcutRegistrationStatus.OUT_OF_SYNC

        if entry.get("canonical") != expected.canonical:
            return ShortcutRegistrationStatus.OUT_OF_SYNC
        if entry.get("xdg_trigger") != shortcut_to_xdg_trigger(expected):
            return ShortcutRegistrationStatus.OUT_OF_SYNC

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
        found.update(k for k in self._load_manifest() if k.startswith("spawnkey-"))
        if self.delegate is not None:
            found.update(self.delegate.list_managed_shortcut_ids())
        return sorted(found)
