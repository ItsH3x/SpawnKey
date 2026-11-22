"""GTK 4 modal dialog to pick an installed .desktop application."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from spawnkey.core.desktop_apps import DesktopApp, discover_desktop_apps


def _make_app_icon(icon_spec: str) -> Gtk.Image:
    """Build a 28px Gtk.Image from a freedesktop icon name or absolute file path."""
    spec = (icon_spec or "").strip()
    if spec and "/" in spec and Path(spec).is_file():
        img = Gtk.Image.new_from_file(spec)
    elif spec:
        img = Gtk.Image.new_from_icon_name(spec)
    else:
        img = Gtk.Image.new_from_icon_name("application-x-executable")
    img.set_pixel_size(28)
    return img


class AppPickerDialog(Gtk.Window):
    """Simple searchable picker listing installed desktop applications."""

    def __init__(
        self,
        parent: Gtk.Window,
        on_selected: Callable[[DesktopApp], None],
    ) -> None:
        super().__init__(
            title="Seleccionar aplicación instalada",
            transient_for=parent,
            modal=True,
            default_width=540,
            default_height=480,
        )
        self._on_selected = on_selected
        self._all_apps = discover_desktop_apps()
        self._filtered_apps: list[DesktopApp] = list(self._all_apps)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Filtrar por nombre, comando o descripción...")
        self._search.connect("search-changed", self._on_search_changed)
        root.append(self._search)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_has_frame(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scrolled)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self._listbox)

        btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_bar.set_halign(Gtk.Align.END)
        root.append(btn_bar)

        btn_cancel = Gtk.Button(label="Cancelar")
        btn_cancel.connect("clicked", lambda _b: self.close())
        btn_choose = Gtk.Button(label="Seleccionar")
        btn_choose.add_css_class("suggested-action")
        btn_choose.connect("clicked", self._on_choose_clicked)

        btn_bar.append(btn_cancel)
        btn_bar.append(btn_choose)

        self._populate("")

    def _populate(self, query: str) -> None:
        while True:
            row = self._listbox.get_row_at_index(0)
            if row is None:
                break
            self._listbox.remove(row)

        needle = query.casefold().strip()
        if needle:
            self._filtered_apps = [
                a
                for a in self._all_apps
                if needle in a.name.casefold()
                or needle in a.desktop_id.casefold()
                or needle in a.exec_cmd.casefold()
                or needle in a.comment.casefold()
            ]
        else:
            self._filtered_apps = list(self._all_apps)

        for app in self._filtered_apps:
            row = Gtk.ListBoxRow()
            row._desktop_app = app  # type: ignore[attr-defined]

            outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            outer.set_margin_top(6)
            outer.set_margin_bottom(6)
            outer.set_margin_start(8)
            outer.set_margin_end(8)

            icon_img = _make_app_icon(app.icon)
            icon_img.set_valign(Gtk.Align.CENTER)
            outer.append(icon_img)

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)

            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl_name = Gtk.Label(label=app.name, xalign=0.0)
            lbl_name.add_css_class("heading")
            lbl_name.set_ellipsize(Pango.EllipsizeMode.END)
            lbl_name.set_hexpand(True)
            title_row.append(lbl_name)

            if app.terminal:
                lbl_term = Gtk.Label(label="[Terminal]")
                lbl_term.add_css_class("spawnkey-badge")
                lbl_term.add_css_class("spawnkey-badge-terminal")
                title_row.append(lbl_term)

            text_box.append(title_row)

            if app.comment:
                lbl_comment = Gtk.Label(label=app.comment, xalign=0.0)
                lbl_comment.add_css_class("dim-label")
                lbl_comment.set_ellipsize(Pango.EllipsizeMode.END)
                text_box.append(lbl_comment)

            lbl_cmd = Gtk.Label(label=f"{app.exec_cmd}  ·  {app.desktop_id}", xalign=0.0)
            lbl_cmd.add_css_class("dim-label")
            lbl_cmd.add_css_class("caption")
            lbl_cmd.set_ellipsize(Pango.EllipsizeMode.END)
            text_box.append(lbl_cmd)

            outer.append(text_box)
            row.set_child(outer)
            self._listbox.append(row)

        first = self._listbox.get_row_at_index(0)
        if first is not None:
            self._listbox.select_row(first)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._populate(entry.get_text())

    def _confirm_row(self, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        app: DesktopApp = row._desktop_app  # type: ignore[attr-defined]
        self._on_selected(app)
        self.close()

    def _on_row_activated(self, _lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._confirm_row(row)

    def _on_choose_clicked(self, _btn: Gtk.Button) -> None:
        self._confirm_row(self._listbox.get_selected_row())
