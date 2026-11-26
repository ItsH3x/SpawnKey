"""Application entry point for SpawnKey GTK 4 GUI."""

from __future__ import annotations

from pathlib import Path
import sys

from spawnkey.i18n import _


def _find_assets_dir() -> Path | None:
    """Locate the directory containing SpawnKey's SVG icon (repo checkout or installed share dir)."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "assets",  # <repo>/assets/spawnkey.svg
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps",
        Path("/usr/share/icons/hicolor/scalable/apps"),
    ]
    for directory in candidates:
        if (directory / "io.github.spawnkey.App.svg").is_file() or (
            directory / "spawnkey.svg"
        ).is_file():
            return directory
    return None


def _configure_app_icon(Gtk, Gdk) -> str:
    """Register SpawnKey's SVG icon with the active GTK 4 IconTheme and set default window icon."""
    display = Gdk.Display.get_default()
    assets_dir = _find_assets_dir()
    icon_name = "spawnkey"
    if display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        if assets_dir is not None:
            theme.add_search_path(str(assets_dir))
        if theme.has_icon("io.github.spawnkey.App"):
            icon_name = "io.github.spawnkey.App"
    Gtk.Window.set_default_icon_name(icon_name)
    return icon_name


def launch_gui(argv: list[str] | None = None) -> int:
    """Start the GTK 4 configurator window and exit cleanly when closed."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk
    except Exception as exc:
        print(
            _("Error: could not load GTK 4 (python-gobject / gtk4): {error}").format(error=exc),
            file=sys.stderr,
        )
        return 1

    from spawnkey.gui.main_window import SpawnKeyWindow

    app = Gtk.Application(application_id="io.github.spawnkey.App")

    def _on_activate(gtk_app: Gtk.Application) -> None:
        icon_name = _configure_app_icon(Gtk, Gdk)
        win = gtk_app.get_active_window()
        if win is None:
            win = SpawnKeyWindow(gtk_app)
            win.set_icon_name(icon_name)
        win.present()

    app.connect("activate", _on_activate)
    return int(app.run(argv if argv is not None else []))
