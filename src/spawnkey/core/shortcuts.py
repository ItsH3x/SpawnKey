"""Backend-agnostic shortcut parsing and canonical normalization."""

from __future__ import annotations

from dataclasses import dataclass


MODIFIER_ALIASES: dict[str, str] = {
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}

# Canonical modifier ordering (e.g. Meta+Ctrl+Alt+Shift+T)
MODIFIER_ORDER: tuple[str, ...] = ("Meta", "Ctrl", "Alt", "Shift")

# Canonical key labels for named keys (lowercase token -> canonical label)
NAMED_KEYS: dict[str, str] = {
    "escape": "Esc",
    "esc": "Esc",
    "tab": "Tab",
    "backspace": "Backspace",
    "return": "Return",
    "enter": "Return",
    "insert": "Ins",
    "ins": "Ins",
    "delete": "Del",
    "del": "Del",
    "home": "Home",
    "end": "End",
    "left": "Left",
    "up": "Up",
    "right": "Right",
    "down": "Down",
    "pageup": "PgUp",
    "pgup": "PgUp",
    "pagedown": "PgDown",
    "pgdown": "PgDown",
    "space": "Space",
    "grave": "`",
    "quoteleft": "`",
    "backtick": "`",
    "minus": "-",
    "equal": "=",
    "plus": "+",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "bracketleft": "[",
    "bracketright": "]",
}

for _n in range(1, 25):
    NAMED_KEYS[f"f{_n}"] = f"F{_n}"


@dataclass(frozen=True)
class ParsedShortcut:
    """Normalized representation of a global keyboard shortcut."""

    modifiers: tuple[str, ...]
    key: str
    canonical: str


def parse_shortcut(shortcut: str) -> ParsedShortcut:
    """Validate and normalize a shortcut string (e.g. 'Super+Shift+t' -> 'Meta+Shift+T')."""
    if not shortcut or not shortcut.strip():
        raise ValueError("El atajo de teclado está vacío.")

    raw_parts = [part.strip() for part in shortcut.split("+") if part.strip()]
    if not raw_parts:
        raise ValueError(f"Atajo inválido: {shortcut!r}")

    mod_names: set[str] = set()
    key_token: str | None = None

    for part in raw_parts:
        low = part.lower()
        if low in MODIFIER_ALIASES:
            mod_names.add(MODIFIER_ALIASES[low])
        else:
            if key_token is not None:
                raise ValueError(f"Atajo inválido '{shortcut}': más de una tecla principal.")
            key_token = part

    if key_token is None:
        raise ValueError(f"Atajo inválido '{shortcut}': falta la tecla principal.")

    key_low = key_token.lower()
    if key_low in NAMED_KEYS:
        key_label = NAMED_KEYS[key_low]
    elif len(key_token) == 1:
        key_label = key_token.upper()
    else:
        raise ValueError(f"Tecla no reconocida en el atajo: '{key_token}'.")

    ordered_mods = tuple(m for m in MODIFIER_ORDER if m in mod_names)
    canonical = "+".join([*ordered_mods, key_label])
    return ParsedShortcut(modifiers=ordered_mods, key=key_label, canonical=canonical)


def normalize_shortcut_key(shortcut: str) -> str:
    """Return canonical shortcut string or stripped lowercase fallback if unparseable."""
    text = (shortcut or "").strip()
    if not text:
        return ""
    try:
        return parse_shortcut(text).canonical
    except ValueError:
        return text.lower()


def find_profile_shortcut_conflicts(
    profiles: list,
    shortcut: str,
    *,
    exclude_profile_id: str | None = None,
) -> list:
    """Return profiles whose enabled shortcut collides with the given shortcut string."""
    target = normalize_shortcut_key(shortcut)
    if not target:
        return []
    conflicts = []
    for profile in profiles:
        if exclude_profile_id and getattr(profile, "id", None) == exclude_profile_id:
            continue
        if not getattr(profile, "shortcut", "") or not getattr(profile, "shortcut_enabled", True):
            continue
        if normalize_shortcut_key(profile.shortcut) == target:
            conflicts.append(profile)
    return conflicts
