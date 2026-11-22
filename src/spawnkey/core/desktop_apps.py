"""Discovery of installed desktop applications (.desktop) and custom executables."""

from __future__ import annotations

from dataclasses import dataclass
import locale
import os
from pathlib import Path
import shlex
import shutil


# Freedesktop Exec field codes removed when launching standalone apps.
_FIELD_CODES = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m"}


@dataclass(frozen=True)
class DesktopApp:
    """Metadata extracted from a freedesktop .desktop entry."""

    desktop_id: str
    name: str
    exec_cmd: str
    raw_exec: str
    icon: str
    wm_class: str
    comment: str
    path: Path
    terminal: bool = False


def clean_exec_command(raw_exec: str) -> str:
    """Strip freedesktop field codes (%u, %F, etc.) while preserving valid arguments."""
    if not raw_exec:
        return ""
    try:
        tokens = shlex.split(raw_exec)
    except ValueError:
        tokens = raw_exec.split()

    cleaned: list[str] = []
    for token in tokens:
        if token in _FIELD_CODES:
            continue
        # Handle tokens containing embedded field codes or escaped '%%'
        replaced = token.replace("%%", "\x00PERCENT\x00")
        for code in _FIELD_CODES:
            replaced = replaced.replace(code, "")
        replaced = replaced.replace("\x00PERCENT\x00", "%")
        if replaced:
            cleaned.append(replaced)

    return shlex.join(cleaned)


def _preferred_locale_keys(base_key: str) -> list[str]:
    lang = os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not lang:
        loc, _ = locale.getlocale()
        lang = loc or ""
    lang = lang.split(".")[0].split("@")[0]
    keys: list[str] = []
    if lang:
        keys.append(f"{base_key}[{lang}]")
        if "_" in lang:
            short = lang.split("_", 1)[0]
            keys.append(f"{base_key}[{short}]")
    keys.append(base_key)
    return keys


def _parse_desktop_entry(path: Path) -> dict[str, str] | None:
    in_section = False
    entries: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section == "Desktop Entry":
                in_section = True
                continue
            if in_section:
                break
            continue
        if not in_section or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in entries:
            entries[key] = value.strip()

    return entries or None


def default_application_dirs() -> list[Path]:
    """Return standard freedesktop application directories in priority order."""
    home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    raw_dirs: list[Path] = [data_home / "applications"]

    xdg_data_dirs = os.environ.get("XDG_DATA_DIRS")
    if xdg_data_dirs:
        for item in xdg_data_dirs.split(":"):
            if item:
                raw_dirs.append(Path(item) / "applications")
    else:
        raw_dirs.extend(
            [
                Path("/usr/local/share/applications"),
                Path("/usr/share/applications"),
            ]
        )

    # Common distro-agnostic Flatpak & Snap export paths
    raw_dirs.extend(
        [
            home / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            Path("/var/lib/snapd/desktop/applications"),
        ]
    )

    unique: list[Path] = []
    seen: set[Path] = set()
    for directory in raw_dirs:
        resolved = directory.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def parse_desktop_file(path: Path) -> DesktopApp | None:
    """Parse a single .desktop file into a DesktopApp if it is launchable and visible."""
    if path.name.startswith("spawnkey-"):
        return None

    data = _parse_desktop_entry(path)
    if not data:
        return None

    if data.get("Type") != "Application":
        return None
    if data.get("NoDisplay", "").lower() == "true":
        return None
    if data.get("Hidden", "").lower() == "true":
        return None

    raw_exec = data.get("Exec", "")
    exec_cmd = clean_exec_command(raw_exec)
    if not exec_cmd:
        return None

    name = ""
    for key in _preferred_locale_keys("Name"):
        if data.get(key):
            name = data[key]
            break
    if not name:
        return None

    comment = ""
    for key in _preferred_locale_keys("Comment"):
        if data.get(key):
            comment = data[key]
            break

    wm_class = data.get("StartupWMClass") or path.stem

    return DesktopApp(
        desktop_id=path.name,
        name=name,
        exec_cmd=exec_cmd,
        raw_exec=raw_exec,
        icon=data.get("Icon", ""),
        wm_class=wm_class,
        comment=comment,
        path=path,
        terminal=data.get("Terminal", "").lower() == "true",
    )


def discover_desktop_apps(
    search_dirs: list[Path] | None = None,
    query: str = "",
) -> list[DesktopApp]:
    """Scan freedesktop directories and return deduplicated launchable applications."""
    dirs = search_dirs if search_dirs is not None else default_application_dirs()
    apps_by_id: dict[str, DesktopApp] = {}

    for directory in dirs:
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.glob("*.desktop"))
        except OSError:
            continue
        for entry in entries:
            if entry.name in apps_by_id:
                continue
            parsed = parse_desktop_file(entry)
            if parsed is not None:
                apps_by_id[entry.name] = parsed

    apps = sorted(apps_by_id.values(), key=lambda app: app.name.casefold())
    if query:
        needle = query.casefold().strip()
        apps = [
            app
            for app in apps
            if needle in app.name.casefold()
            or needle in app.desktop_id.casefold()
            or needle in app.comment.casefold()
        ]
    return apps


def validate_executable_command(command: str) -> tuple[bool, str]:
    """Validate that a user-provided executable path or command can be invoked."""
    if not command or not command.strip():
        return False, "El comando está vacío."
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return False, f"Sintaxis inválida en el comando: {exc}"
    if not tokens:
        return False, "El comando está vacío."

    binary = os.path.expanduser(tokens[0])
    if "/" in binary:
        candidate = Path(binary)
        if not candidate.exists():
            return False, f"No existe el archivo: {candidate}"
        if not candidate.is_file():
            return False, f"La ruta no es un archivo ejecutable: {candidate}"
        if not os.access(candidate, os.X_OK):
            return False, f"El archivo no tiene permiso de ejecución: {candidate}"
        return True, str(candidate)

    resolved = shutil.which(binary)
    if not resolved:
        return False, f"No se encontró el ejecutable '{binary}' en PATH."
    return True, resolved


# Common terminal emulators and the flag they expect before the command argv.
_FALLBACK_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("konsole", ("-e",)),
    ("alacritty", ("-e",)),
    ("kitty", ("-e",)),
    ("foot", ("-e",)),
    ("wezterm", ("start", "--")),
    ("gnome-terminal", ("--",)),
    ("xfce4-terminal", ("-e",)),
    ("tilix", ("-e",)),
    ("lxterminal", ("-e",)),
    ("mate-terminal", ("-e",)),
    ("urxvt", ("-e",)),
    ("xterm", ("-e",)),
)

_TERMINAL_EXEC_FLAGS: dict[str, tuple[str, ...]] = {
    name: flags for name, flags in _FALLBACK_TERMINALS
}


def wrap_terminal_command(
    exec_cmd: str,
    *,
    env: dict[str, str] | None = None,
    which_fn=None,
) -> str:
    """Wrap a Terminal=true command using xdg-terminal-exec, $TERMINAL, or a PATH fallback."""
    if not exec_cmd or not exec_cmd.strip():
        return exec_cmd

    try:
        cmd_tokens = shlex.split(exec_cmd)
    except ValueError:
        cmd_tokens = exec_cmd.split()
    if not cmd_tokens:
        return exec_cmd

    lookup = which_fn or shutil.which
    environ = os.environ if env is None else env

    # 1. Freedesktop standard xdg-terminal-exec
    if lookup("xdg-terminal-exec"):
        return shlex.join(["xdg-terminal-exec", "--", *cmd_tokens])

    # 2. User-configured $TERMINAL environment variable
    env_term = (environ.get("TERMINAL") or "").strip()
    if env_term:
        try:
            term_tokens = shlex.split(env_term)
        except ValueError:
            term_tokens = env_term.split()
        if term_tokens:
            term_bin = os.path.expanduser(term_tokens[0])
            exists = (
                (Path(term_bin).is_file() and os.access(term_bin, os.X_OK))
                if "/" in term_bin
                else bool(lookup(term_bin))
            )
            if exists:
                base = Path(term_bin).name
                flags = _TERMINAL_EXEC_FLAGS.get(base, ("-e",))
                return shlex.join([*term_tokens, *flags, *cmd_tokens])

    # 3. Ordered fallback over common terminal emulators in PATH
    for candidate, flags in _FALLBACK_TERMINALS:
        if lookup(candidate):
            return shlex.join([candidate, *flags, *cmd_tokens])

    return exec_cmd
