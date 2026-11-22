"""Keyboard shortcut capture helper and GTK 4 widget."""

from __future__ import annotations

from typing import Callable

from spawnkey.core.shortcuts import parse_shortcut


# GDK key names that represent standalone modifier keys (ignored as primary key)
_MODIFIER_KEY_NAMES = {
    "Shift_L",
    "Shift_R",
    "Control_L",
    "Control_R",
    "Alt_L",
    "Alt_R",
    "Meta_L",
    "Meta_R",
    "Super_L",
    "Super_R",
    "Hyper_L",
    "Hyper_R",
    "ISO_Level3_Shift",
    "Caps_Lock",
    "Num_Lock",
}

# Map common GDK keyval names to SpawnKey/KDE canonical key tokens
_GDK_KEY_MAP = {
    "space": "Space",
    "Return": "Return",
    "KP_Enter": "Return",
    "Tab": "Tab",
    "ISO_Left_Tab": "Tab",
    "BackSpace": "Backspace",
    "Delete": "Del",
    "Insert": "Ins",
    "Home": "Home",
    "End": "End",
    "Left": "Left",
    "Right": "Right",
    "Up": "Up",
    "Down": "Down",
    "Page_Up": "PgUp",
    "Page_Down": "PgDown",
    "grave": "grave",
    "asciitilde": "grave",
    "dead_grave": "grave",
    "minus": "minus",
    "equal": "equal",
    "plus": "plus",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "bracketleft": "bracketleft",
    "bracketright": "bracketright",
}


def key_event_to_shortcut(
    key_name: str | None,
    *,
    meta: bool = False,
    ctrl: bool = False,
    alt: bool = False,
    shift: bool = False,
) -> str | None:
    """Convert a normalized GDK key press into a canonical shortcut string, or None."""
    if not key_name or key_name in _MODIFIER_KEY_NAMES:
        return None
    if key_name == "Escape" and not (meta or ctrl or alt or shift):
        return None

    mapped = _GDK_KEY_MAP.get(key_name)
    if mapped is None:
        if len(key_name) == 1 and key_name.isalnum():
            mapped = key_name.upper()
        elif key_name.startswith("F") and key_name[1:].isdigit():
            mapped = key_name
        else:
            return None

    parts: list[str] = []
    if meta:
        parts.append("Meta")
    if ctrl:
        parts.append("Ctrl")
    if alt:
        parts.append("Alt")
    if shift:
        parts.append("Shift")
    parts.append(mapped)

    try:
        return parse_shortcut("+".join(parts)).canonical
    except ValueError:
        return None


def build_shortcut_capture_widget(
    on_changed: Callable[[str], None],
) -> tuple[object, Callable[[str], None], Callable[[], str]]:
    """Create a GTK 4 shortcut capture row: returns (widget, set_shortcut, get_shortcut)."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    current = {"value": "", "capturing": False}

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

    entry = Gtk.Entry()
    entry.set_hexpand(True)
    entry.set_placeholder_text("Ej. Meta+Shift+T (o pulsa Capturar)")

    btn_capture = Gtk.ToggleButton(label="Capturar")
    btn_clear = Gtk.Button(label="Borrar")

    box.append(entry)
    box.append(btn_capture)
    box.append(btn_clear)

    def _finish_capture() -> None:
        current["capturing"] = False
        if btn_capture.get_active():
            btn_capture.set_active(False)
        btn_capture.set_label("Capturar")

    def set_shortcut(text: str) -> None:
        current["value"] = text
        if entry.get_text() != text:
            entry.set_text(text)

    def get_shortcut() -> str:
        return entry.get_text().strip()

    def _on_entry_changed(_editable: Gtk.Editable) -> None:
        if not current["capturing"]:
            current["value"] = entry.get_text().strip()
            on_changed(current["value"])

    def _on_capture_toggled(toggle: Gtk.ToggleButton) -> None:
        if toggle.get_active():
            current["capturing"] = True
            toggle.set_label("Pulsa combinación (Esc cancela)")
            toggle.grab_focus()
        else:
            current["capturing"] = False
            toggle.set_label("Capturar")

    def _on_clear_clicked(_btn: Gtk.Button) -> None:
        _finish_capture()
        set_shortcut("")
        on_changed("")

    def _on_key_pressed(
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if not current["capturing"]:
            return False

        key_name = Gdk.keyval_name(keyval)
        if key_name == "Escape" and not (
            state
            & (
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.ALT_MASK
                | Gdk.ModifierType.SHIFT_MASK
                | Gdk.ModifierType.SUPER_MASK
                | Gdk.ModifierType.META_MASK
            )
        ):
            _finish_capture()
            return True

        meta = bool(state & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK))
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        combo = key_event_to_shortcut(key_name, meta=meta, ctrl=ctrl, alt=alt, shift=shift)
        if combo is None:
            return True

        _finish_capture()
        set_shortcut(combo)
        on_changed(combo)
        return True

    key_ctrl = Gtk.EventControllerKey()
    key_ctrl.connect("key-pressed", _on_key_pressed)
    btn_capture.add_controller(key_ctrl)

    entry.connect("changed", _on_entry_changed)
    btn_capture.connect("toggled", _on_capture_toggled)
    btn_clear.connect("clicked", _on_clear_clicked)

    return box, set_shortcut, get_shortcut
