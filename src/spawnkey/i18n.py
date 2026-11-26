"""Lightweight stdlib-only internationalization (gettext) helpers for SpawnKey."""

from __future__ import annotations

from collections.abc import Mapping
import gettext
import locale
import os
from pathlib import Path
import struct

DOMAIN = "spawnkey"

_TRANSLATION_CACHE: dict[
    tuple[str, str, str, str, tuple[str, ...]],
    gettext.NullTranslations,
] = {}


def locale_search_dirs(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return locale search roots in priority order."""
    environ = os.environ if env is None else env
    xdg_data_home = environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return (
        Path(__file__).resolve().parent / "locale",
        Path(xdg_data_home) / "locale",
        Path("/usr/local/share/locale"),
        Path("/usr/share/locale"),
    )


def resolve_active_language(env: Mapping[str, str] | None = None) -> str:
    """Resolve active UI language ('es' or 'en') from environment/locale."""
    environ = os.environ if env is None else env
    raw = (
        environ.get("LC_ALL")
        or environ.get("LC_MESSAGES")
        or environ.get("LANG")
        or ""
    ).strip()
    if not raw:
        try:
            loc, _ = locale.getlocale()
            raw = (loc or "").strip()
        except ValueError:
            raw = ""
    cleaned = raw.split(".", 1)[0].split("@", 1)[0].lower()
    if cleaned.startswith("es"):
        return "es"
    return "en"


def clear_translation_cache() -> None:
    """Clear the cached gettext translation catalog instances."""
    _TRANSLATION_CACHE.clear()


def _get_translations(env: Mapping[str, str] | None = None) -> gettext.NullTranslations:
    environ = os.environ if env is None else env
    lc_all = environ.get("LC_ALL", "")
    lc_messages = environ.get("LC_MESSAGES", "")
    lang_env = environ.get("LANG", "")
    xdg_data = environ.get("XDG_DATA_HOME", "")
    dirs = locale_search_dirs(environ)
    cache_key = (
        lc_all,
        lc_messages,
        lang_env,
        xdg_data,
        tuple(str(d) for d in dirs),
    )

    cached = _TRANSLATION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    active_lang = resolve_active_language(environ)
    translation: gettext.NullTranslations = gettext.NullTranslations()

    if active_lang != "en":
        for root in dirs:
            mo_path = root / active_lang / "LC_MESSAGES" / f"{DOMAIN}.mo"
            if not mo_path.is_file():
                continue
            try:
                with mo_path.open("rb") as handle:
                    translation = gettext.GNUTranslations(handle)
                break
            except (OSError, ValueError):
                continue

    _TRANSLATION_CACHE[cache_key] = translation
    return translation


def _(message: str) -> str:
    """Translate a singular message using the active SpawnKey locale catalog."""
    if not message:
        return message
    return _get_translations().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a pluralizable message using the active SpawnKey locale catalog."""
    return _get_translations().ngettext(singular, plural, n)


def _unescape_po_quoted(raw: str) -> str:
    """Parse a PO quoted string literal (e.g. '"hello\\n"') into Python str."""
    text = raw.strip()
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        raise ValueError(f"Invalid PO quoted string: {raw!r}")
    inner = text[1:-1]
    out: list[str] = []
    idx = 0
    length = len(inner)
    while idx < length:
        ch = inner[idx]
        if ch == "\\" and idx + 1 < length:
            nxt = inner[idx + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == '"':
                out.append('"')
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append(nxt)
            idx += 2
        else:
            out.append(ch)
            idx += 1
    return "".join(out)


def parse_po_file(po_path: Path) -> dict[str, str]:
    """Parse a GNU .po file into a {msgid_key: msgstr_value} mapping.

    Plural entries use ``f"{msgid}\\x00{msgid_plural}"`` as the key and
    ``"\\x00".join(msgstr_forms)`` as the value, matching GNU .mo layout.
    """
    entries: dict[str, str] = {}
    current_msgid: list[str] | None = None
    current_plural: list[str] | None = None
    current_msgstr: list[str] | None = None
    current_msgstr_plurals: dict[int, list[str]] = {}
    active_target: list[str] | None = None
    is_fuzzy = False

    def _flush() -> None:
        nonlocal current_msgid, current_plural, current_msgstr
        nonlocal current_msgstr_plurals, active_target, is_fuzzy
        if current_msgid is None:
            is_fuzzy = False
            return
        msgid_str = "".join(current_msgid)
        if not is_fuzzy:
            if current_plural is not None:
                plural_str = "".join(current_plural)
                if current_msgstr_plurals:
                    max_idx = max(current_msgstr_plurals)
                    forms = [
                        "".join(current_msgstr_plurals.get(i, []))
                        for i in range(max_idx + 1)
                    ]
                    if any(forms):
                        entries[f"{msgid_str}\x00{plural_str}"] = "\x00".join(forms)
            elif current_msgstr is not None:
                msgstr_str = "".join(current_msgstr)
                if msgid_str == "" or msgstr_str != "":
                    entries[msgid_str] = msgstr_str

        current_msgid = None
        current_plural = None
        current_msgstr = None
        current_msgstr_plurals = {}
        active_target = None
        is_fuzzy = False

    content = po_path.read_text(encoding="utf-8")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            _flush()
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                is_fuzzy = True
            continue

        if line.startswith("msgid_plural "):
            current_plural = [_unescape_po_quoted(line[len("msgid_plural ") :])]
            active_target = current_plural
        elif line.startswith("msgid "):
            if current_msgid is not None:
                _flush()
            current_msgid = [_unescape_po_quoted(line[len("msgid ") :])]
            active_target = current_msgid
        elif line.startswith("msgstr["):
            close_idx = line.find("]")
            if close_idx != -1:
                idx_num = int(line[len("msgstr[") : close_idx])
                rest = line[close_idx + 1 :].strip()
                buf = [_unescape_po_quoted(rest)]
                current_msgstr_plurals[idx_num] = buf
                active_target = buf
        elif line.startswith("msgstr "):
            current_msgstr = [_unescape_po_quoted(line[len("msgstr ") :])]
            active_target = current_msgstr
        elif line.startswith('"') and active_target is not None:
            active_target.append(_unescape_po_quoted(line))

    _flush()
    return entries


_DEFAULT_MO_HEADER = (
    "Content-Type: text/plain; charset=UTF-8\n"
    "Plural-Forms: nplurals=2; plural=(n != 1);\n"
)


def compile_po_to_mo(po_path: Path, mo_path: Path) -> Path:
    """Compile a GNU .po file into a binary little-endian .mo catalog."""
    catalog = parse_po_file(po_path)
    if "" not in catalog:
        catalog[""] = _DEFAULT_MO_HEADER
    else:
        hdr = catalog[""]
        if "Content-Type:" not in hdr:
            hdr = f"Content-Type: text/plain; charset=UTF-8\n{hdr}"
        if "Plural-Forms:" not in hdr:
            hdr = f"{hdr.rstrip()}\nPlural-Forms: nplurals=2; plural=(n != 1);\n"
        catalog[""] = hdr

    encoded_pairs = sorted(
        (key.encode("utf-8"), val.encode("utf-8"))
        for key, val in catalog.items()
    )

    count = len(encoded_pairs)
    orig_table_offset = 28
    trans_table_offset = orig_table_offset + count * 8
    data_offset = trans_table_offset + count * 8

    orig_blobs = bytearray()
    trans_blobs = bytearray()
    orig_entries: list[tuple[int, int]] = []
    trans_entries: list[tuple[int, int]] = []

    orig_cursor = data_offset
    for orig_b, _ in encoded_pairs:
        orig_entries.append((len(orig_b), orig_cursor))
        orig_blobs.extend(orig_b)
        orig_blobs.append(0)
        orig_cursor += len(orig_b) + 1

    trans_cursor = orig_cursor
    for _, trans_b in encoded_pairs:
        trans_entries.append((len(trans_b), trans_cursor))
        trans_blobs.extend(trans_b)
        trans_blobs.append(0)
        trans_cursor += len(trans_b) + 1

    output = bytearray()
    output.extend(
        struct.pack(
            "<7I",
            0x950412DE,
            0,
            count,
            orig_table_offset,
            trans_table_offset,
            0,
            0,
        )
    )
    for length, offset in orig_entries:
        output.extend(struct.pack("<2I", length, offset))
    for length, offset in trans_entries:
        output.extend(struct.pack("<2I", length, offset))
    output.extend(orig_blobs)
    output.extend(trans_blobs)

    mo_path.parent.mkdir(parents=True, exist_ok=True)
    mo_path.write_bytes(bytes(output))
    clear_translation_cache()
    return mo_path
