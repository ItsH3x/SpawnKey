"""Main GTK 4 window for SpawnKey: compact dropdown editor and shortcut manager."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, Gtk, Pango

from spawnkey.backends.base import DiagnosticItem, ShortcutRegistrationStatus
from spawnkey.core.desktop_apps import (
    DesktopApp,
    validate_executable_command,
    wrap_terminal_command,
)
from spawnkey.core.geometry import compute_animation_duration_ms, select_monitor_by_policy
from spawnkey.core.manager import ManagedEntry, SpawnKeyManager
from spawnkey.core.models import (
    AnchorH,
    AnchorV,
    AnimationType,
    DropdownProfile,
    LifecycleMode,
    MonitorPolicy,
    MonitorRect,
    PositionMode,
    SizeUnit,
    SlideDirection,
    SourceType,
)
from spawnkey.gui.app_picker import AppPickerDialog
from spawnkey.gui.preview_widget import PreviewWidget
from spawnkey.gui.shortcut_capture import build_shortcut_capture_widget
from spawnkey.i18n import _, ngettext


_STATUS_LABELS = {
    ShortcutRegistrationStatus.ACTIVE: "Active",
    ShortcutRegistrationStatus.UNASSIGNED: "No shortcut",
    ShortcutRegistrationStatus.DISABLED: "Disabled",
    ShortcutRegistrationStatus.OUT_OF_SYNC: "Out of sync",
}

_STATUS_CSS = {
    ShortcutRegistrationStatus.ACTIVE: "spawnkey-badge-active",
    ShortcutRegistrationStatus.UNASSIGNED: "spawnkey-badge-unassigned",
    ShortcutRegistrationStatus.DISABLED: "spawnkey-badge-disabled",
    ShortcutRegistrationStatus.OUT_OF_SYNC: "spawnkey-badge-warn",
}

_DIRECTIONS = [
    (SlideDirection.TOP, "Top"),
    (SlideDirection.BOTTOM, "Bottom"),
    (SlideDirection.LEFT, "Left"),
    (SlideDirection.RIGHT, "Right"),
]

_ANIMATIONS = [
    (AnimationType.SLIDE, "Slide"),
    (AnimationType.FADE, "Fade"),
    (AnimationType.SLIDE_FADE, "Slide + fade"),
    (AnimationType.NONE, "Instant"),
]

_MONITOR_POLICIES = [
    (MonitorPolicy.CURSOR, "Cursor monitor"),
    (MonitorPolicy.FOCUSED, "Focused monitor"),
    (MonitorPolicy.PRIMARY, "Primary monitor"),
]

# Compact 3x3 directional symbols with descriptive tooltips
_ANCHOR_GRID = [
    (AnchorV.TOP, AnchorH.LEFT, "↖", "Top left"),
    (AnchorV.TOP, AnchorH.CENTER, "↑", "Top center"),
    (AnchorV.TOP, AnchorH.RIGHT, "↗", "Top right"),
    (AnchorV.CENTER, AnchorH.LEFT, "←", "Center left"),
    (AnchorV.CENTER, AnchorH.CENTER, "•", "Center"),
    (AnchorV.CENTER, AnchorH.RIGHT, "→", "Center right"),
    (AnchorV.BOTTOM, AnchorH.LEFT, "↙", "Bottom left"),
    (AnchorV.BOTTOM, AnchorH.CENTER, "↓", "Bottom center"),
    (AnchorV.BOTTOM, AnchorH.RIGHT, "↘", "Bottom right"),
]

_APP_CSS = b"""
.spawnkey-badge {
    font-size: 0.78em;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 999px;
}
.spawnkey-badge-active {
    background-color: alpha(#2ec27e, 0.22);
    color: #2ec27e;
}
.spawnkey-badge-warn {
    background-color: alpha(#e5a50a, 0.24);
    color: #e5a50a;
}
.spawnkey-badge-unassigned {
    background-color: alpha(currentColor, 0.10);
    color: alpha(currentColor, 0.65);
}
.spawnkey-badge-disabled {
    background-color: alpha(#e01b24, 0.18);
    color: #e01b24;
}
.spawnkey-badge-terminal {
    background-color: alpha(#3584e4, 0.20);
    color: #3584e4;
}
.spawnkey-conflict-warn {
    color: #e5a50a;
    font-weight: 600;
}
.spawnkey-mode-desc {
    margin-left: 24px;
    margin-bottom: 4px;
    font-size: 0.88em;
}
"""


def _install_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_APP_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def _make_json_file_filters() -> tuple[Gio.ListStore, Gtk.FileFilter]:
    """Create a Gio.ListStore of JSON and wildcard Gtk.FileFilters."""
    json_filter = Gtk.FileFilter()
    json_filter.set_name(_("JSON files (*.json)"))
    json_filter.add_pattern("*.json")
    json_filter.add_mime_type("application/json")

    all_filter = Gtk.FileFilter()
    all_filter.set_name(_("All files"))
    all_filter.add_pattern("*")

    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(json_filter)
    filters.append(all_filter)
    return filters, json_filter


def detect_gdk_monitors(
    display=None,
    surface=None,
) -> tuple[list[MonitorRect], int, int | None]:
    """Read real monitor geometries from Gdk.Display.get_monitors() without DE-specific binaries."""
    disp = display if display is not None else Gdk.Display.get_default()
    if disp is None or not hasattr(disp, "get_monitors"):
        return [], 0, None

    model = disp.get_monitors()
    if model is None or not hasattr(model, "get_n_items"):
        return [], 0, None

    active_gdk_mon = None
    if surface is not None and hasattr(disp, "get_monitor_at_surface"):
        try:
            active_gdk_mon = disp.get_monitor_at_surface(surface)
        except Exception:
            active_gdk_mon = None

    monitors: list[MonitorRect] = []
    primary_idx = 0
    focused_idx: int | None = None

    count = int(model.get_n_items())
    for idx in range(count):
        item = model.get_item(idx)
        if item is None or not hasattr(item, "get_geometry"):
            continue
        geom = item.get_geometry()
        w = int(getattr(geom, "width", 0))
        h = int(getattr(geom, "height", 0))
        if w <= 1 or h <= 1:
            continue
        x = int(getattr(geom, "x", 0))
        y = int(getattr(geom, "y", 0))
        connector = ""
        if hasattr(item, "get_connector"):
            connector = item.get_connector() or ""
        if not connector and hasattr(item, "get_model"):
            connector = item.get_model() or ""
        name = str(connector or f"monitor-{len(monitors)}")
        cur_idx = len(monitors)
        if x == 0 and y == 0 and primary_idx == 0:
            primary_idx = cur_idx
        if active_gdk_mon is not None and item == active_gdk_mon:
            focused_idx = cur_idx
        monitors.append(MonitorRect(x=x, y=y, width=w, height=h, name=name))

    return monitors, primary_idx, focused_idx


def query_gdk_monitor(
    policy: MonitorPolicy = MonitorPolicy.CURSOR,
    *,
    display=None,
    window=None,
) -> MonitorRect | None:
    """Resolve the active MonitorRect via Gdk.Display.get_monitors() honoring MonitorPolicy."""
    surface = None
    if window is not None and hasattr(window, "get_surface"):
        try:
            surface = window.get_surface()
        except Exception:
            surface = None
    monitors, primary_idx, focused_idx = detect_gdk_monitors(display=display, surface=surface)
    if not monitors:
        return None
    return select_monitor_by_policy(
        monitors,
        policy,
        primary_index=primary_idx,
        focused_index=focused_idx,
    )


class _DiagnosticsDialog(Gtk.Window):
    """Modal window showing SpawnKey environment, backend, and shortcut diagnostics."""

    def __init__(self, parent: Gtk.Window, manager: SpawnKeyManager) -> None:
        super().__init__(
            title=_("SpawnKey Diagnostics"),
            transient_for=parent,
            modal=True,
            default_width=640,
            default_height=460,
        )
        self._manager = manager
        self._report_text = ""

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        self._summary_label = Gtk.Label(label="", xalign=0.0)
        self._summary_label.add_css_class("heading")
        self._summary_label.set_wrap(True)
        root.append(self._summary_label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_has_frame(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scrolled)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.set_child(self._list_box)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._copy_feedback = Gtk.Label(label="", xalign=0.0)
        self._copy_feedback.add_css_class("dim-label")
        self._copy_feedback.set_hexpand(True)
        footer.append(self._copy_feedback)

        btn_refresh = Gtk.Button(label=_("Refresh"))
        btn_refresh.connect("clicked", lambda _b: self._populate())
        btn_copy = Gtk.Button(label=_("Copy"))
        btn_copy.connect("clicked", self._on_copy_clicked)
        btn_close = Gtk.Button(label=_("Close"))
        btn_close.add_css_class("suggested-action")
        btn_close.connect("clicked", lambda _b: self.close())

        footer.append(btn_refresh)
        footer.append(btn_copy)
        footer.append(btn_close)
        root.append(footer)

        self._populate()

    def _populate(self) -> None:
        while True:
            row = self._list_box.get_row_at_index(0)
            if row is None:
                break
            self._list_box.remove(row)

        self._copy_feedback.set_text("")
        items: list[DiagnosticItem] = self._manager.diagnose()

        badge_labels = {
            "ok": _("OK"),
            "warn": _("WARN"),
            "error": _("ERROR"),
        }
        badge_css = {
            "ok": "spawnkey-badge-active",
            "warn": "spawnkey-badge-warn",
            "error": "spawnkey-badge-disabled",
        }
        counts = {"ok": 0, "warn": 0, "error": 0}
        report_lines: list[str] = []

        for level, check, detail in items:
            norm = level.lower()
            if norm not in counts:
                norm = "warn"
            counts[norm] += 1
            badge_text = badge_labels[norm]
            report_lines.append(f"[{badge_text}] {check}: {detail}")

            row = Gtk.ListBoxRow()
            outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            outer.set_margin_top(6)
            outer.set_margin_bottom(6)
            outer.set_margin_start(8)
            outer.set_margin_end(8)

            lbl_badge = Gtk.Label(label=badge_text)
            lbl_badge.set_valign(Gtk.Align.START)
            lbl_badge.add_css_class("spawnkey-badge")
            lbl_badge.add_css_class(badge_css[norm])
            outer.append(lbl_badge)

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)

            lbl_check = Gtk.Label(label=check, xalign=0.0)
            lbl_check.add_css_class("heading")
            lbl_check.set_wrap(True)
            text_box.append(lbl_check)

            lbl_detail = Gtk.Label(label=detail, xalign=0.0)
            lbl_detail.add_css_class("dim-label")
            lbl_detail.set_wrap(True)
            lbl_detail.set_selectable(True)
            text_box.append(lbl_detail)

            outer.append(text_box)
            row.set_child(outer)
            self._list_box.append(row)

        summary = _("Summary: {ok} OK · {warn} WARN · {error} ERROR").format(
            ok=counts["ok"],
            warn=counts["warn"],
            error=counts["error"],
        )
        self._summary_label.set_text(summary)
        report_lines.append("")
        report_lines.append(summary)
        self._report_text = "\n".join(report_lines)

    def _on_copy_clicked(self, _btn: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            self._copy_feedback.set_text(_("Clipboard unavailable."))
            return
        display.get_clipboard().set(self._report_text)
        self._copy_feedback.set_text(_("Diagnostic report copied to clipboard."))


class SpawnKeyWindow(Gtk.ApplicationWindow):
    """Compact two-pane GTK 4 window: managed dropdown list + tabbed profile editor."""

    def __init__(self, app: Gtk.Application, manager: SpawnKeyManager | None = None) -> None:
        super().__init__(
            application=app,
            title=_("SpawnKey — Dropdown & Shortcut Manager"),
            default_width=860,
            default_height=680,
        )
        _install_css()
        self._manager = manager or SpawnKeyManager()
        self._monitors, self._monitor = self._resolve_gui_monitors(MonitorPolicy.CURSOR)

        self._loading_form = False
        self._dirty = False
        self._suppress_row_select = False
        self._current_id: str | None = None
        self._current_source_type: SourceType = SourceType.EXECUTABLE
        self._current_desktop_id: str | None = None
        self._current_match_hint: str = ""
        self._selected_anchor_h: AnchorH = AnchorH.CENTER
        self._selected_anchor_v: AnchorV = AnchorV.TOP
        self._anchor_buttons: dict[tuple[AnchorV, AnchorH], Gtk.ToggleButton] = {}

        self._build_ui()
        self._refresh_list(select_first=True)
        self.connect("map", self._on_window_mapped)

        if not self._manager.backend_available:
            self._set_status(
                _("Warning: {error}").format(error=self._manager.backend_error),
                error=True,
            )
        else:
            caps = self._manager.capabilities
            if caps.backend_hint:
                self._set_status(f"{self._manager.env_info.backend.value} · {caps.backend_hint}")
                self._status_label.set_tooltip_text(caps.backend_hint)
            else:
                self._set_status(
                    _("Ready ({backend} · monitor {width}×{height})").format(
                        backend=self._manager.env_info.backend.value,
                        width=self._monitor.width,
                        height=self._monitor.height,
                    )
                )

    def _on_window_mapped(self, *_args) -> None:
        policy = _MONITOR_POLICIES[self._dd_monitor_policy.get_selected()][0]
        self._refresh_active_monitor(policy)
        self._update_preview()

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(outer)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        paned.set_position(250)
        outer.append(paned)

        left = self._build_left_pane()
        left.set_size_request(220, -1)
        right = self._build_right_pane()
        right.set_size_request(460, -1)

        paned.set_start_child(left)
        paned.set_end_child(right)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)

        # Bottom status bar
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_bar.set_margin_top(6)
        status_bar.set_margin_bottom(6)
        status_bar.set_margin_start(12)
        status_bar.set_margin_end(12)
        self._status_label = Gtk.Label(label="", xalign=0.0)
        self._status_label.set_hexpand(True)
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._status_label.add_css_class("dim-label")
        status_bar.append(self._status_label)

        self._btn_diagnostics = Gtk.Button(label=_("Diagnostics"))
        self._btn_diagnostics.add_css_class("flat")
        self._btn_diagnostics.set_tooltip_text(
            _("Inspect environment, compositor backend, scripts, and shortcut status")
        )
        self._btn_diagnostics.connect("clicked", self._on_diagnostics_clicked)
        status_bar.append(self._btn_diagnostics)

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        outer.append(status_bar)

    def _build_left_pane(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=_("Profiles"), xalign=0.0)
        title.set_hexpand(True)
        title.add_css_class("title-4")

        # + New menu button with quick recipes
        self._btn_new = Gtk.MenuButton(label=_("+ New"))
        self._btn_new.add_css_class("suggested-action")
        self._new_popover = self._build_new_recipe_popover()
        self._btn_new.set_popover(self._new_popover)

        header.append(title)
        header.append(self._btn_new)
        box.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_has_frame(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.connect("row-selected", self._on_row_selected)
        scrolled.set_child(self._list_box)
        box.append(scrolled)

        # Orphaned shortcut notice (hidden unless orphans exist)
        self._orphan_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._orphan_label = Gtk.Label(label="", xalign=0.0)
        self._orphan_label.set_hexpand(True)
        self._orphan_label.set_wrap(True)
        btn_clean = Gtk.Button(label=_("Clean up"))
        btn_clean.connect("clicked", self._on_cleanup_orphans_clicked)
        self._orphan_box.append(self._orphan_label)
        self._orphan_box.append(btn_clean)
        self._orphan_box.set_visible(False)
        box.append(self._orphan_box)

        # Compact quick actions grid (profile actions + import/export)
        actions = Gtk.Grid(column_spacing=6, row_spacing=6)
        actions.set_column_homogeneous(True)

        self._btn_list_test = Gtk.Button(label=_("Test"))
        self._btn_list_test.connect("clicked", self._on_test_clicked)

        self._btn_list_duplicate = Gtk.Button(label=_("Duplicate"))
        self._btn_list_duplicate.connect("clicked", self._on_duplicate_clicked)

        self._btn_list_rm_shortcut = Gtk.Button(label=_("Remove shortcut"))
        self._btn_list_rm_shortcut.connect("clicked", self._on_remove_shortcut_only_clicked)

        self._btn_list_delete = Gtk.Button(label=_("Delete"))
        self._btn_list_delete.add_css_class("destructive-action")
        self._btn_list_delete.connect("clicked", self._on_delete_profile_clicked)

        self._btn_import = Gtk.Button(label=_("Import..."))
        self._btn_import.connect("clicked", self._on_import_clicked)

        self._btn_export = Gtk.Button(label=_("Export..."))
        self._btn_export.connect("clicked", self._on_export_clicked)

        actions.attach(self._btn_list_test, 0, 0, 1, 1)
        actions.attach(self._btn_list_duplicate, 1, 0, 1, 1)
        actions.attach(self._btn_list_rm_shortcut, 0, 1, 1, 1)
        actions.attach(self._btn_list_delete, 1, 1, 1, 1)
        actions.attach(self._btn_import, 0, 2, 1, 1)
        actions.attach(self._btn_export, 1, 2, 1, 1)
        box.append(actions)

        return box

    def _build_new_recipe_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)
        vbox.set_margin_start(6)
        vbox.set_margin_end(6)

        recipes = [
            ("blank", _("Blank profile"), _("Default initial settings")),
            ("top_terminal", _("Top terminal"), _("Persistent · top · 85×45%")),
            ("tray_panel", _("Tray side panel"), _("Tray · right · 38×82% · margin 8px")),
            ("ephemeral_center", _("Centered tool"), _("Ephemeral · center · 55×60% · fade")),
        ]
        for key, title, subtitle in recipes:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            lbl_t = Gtk.Label(label=title, xalign=0.0)
            lbl_t.add_css_class("heading")
            lbl_s = Gtk.Label(label=subtitle, xalign=0.0)
            lbl_s.add_css_class("dim-label")
            lbl_s.add_css_class("caption")
            inner.append(lbl_t)
            inner.append(lbl_s)
            btn.set_child(inner)
            btn.connect("clicked", self._on_new_recipe_clicked, key)
            vbox.append(btn)

        pop.set_child(vbox)
        return pop

    def _section_label(self, text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.add_css_class("heading")
        lbl.set_margin_top(4)
        return lbl

    @staticmethod
    def _pair_box(label_text: str, control: Gtk.Widget) -> Gtk.Box:
        """Group a label immediately next to its control without stretching column width."""
        pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pair.set_halign(Gtk.Align.START)
        pair.set_hexpand(False)
        lbl = Gtk.Label(label=label_text, xalign=0.0)
        control.set_hexpand(False)
        control.set_halign(Gtk.Align.START)
        pair.append(lbl)
        pair.append(control)
        return pair

    def _build_right_pane(self) -> Gtk.Widget:
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        wrapper.set_margin_top(10)
        wrapper.set_margin_bottom(10)
        wrapper.set_margin_start(12)
        wrapper.set_margin_end(12)

        # Compact live preview pinned at the top so changes are always visible
        self._preview = PreviewWidget(
            self._monitor,
            on_custom_drag=self._on_preview_drag,
            monitors=self._monitors,
        )
        wrapper.append(self._preview)

        # Tabbed editor (Gtk.Stack + Gtk.StackSwitcher)
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_halign(Gtk.Align.CENTER)
        wrapper.append(switcher)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self._stack)
        wrapper.append(scrolled)

        self._stack.add_titled(self._build_tab_app(), "app", _("Application"))
        self._stack.add_titled(self._build_tab_geometry(), "geometry", _("Geometry"))
        self._stack.add_titled(self._build_tab_style(), "style", _("Style"))
        self._stack.add_titled(self._build_tab_behavior(), "behavior", _("Window & shortcut"))

        # Footer buttons
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        wrapper.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        wrapper.append(footer)

        self._btn_test = Gtk.Button(label=_("Test now"))
        self._btn_test.connect("clicked", self._on_test_clicked)

        self._btn_save = Gtk.Button(label=_("Save & apply"))
        self._btn_save.add_css_class("suggested-action")
        self._btn_save.connect("clicked", self._on_save_clicked)

        footer.append(self._btn_test)
        footer.append(self._btn_save)

        self._select_anchor(AnchorV.TOP, AnchorH.CENTER)
        self._apply_backend_capabilities()
        return wrapper

    def _build_tab_app(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(6)
        page.set_margin_end(4)

        app_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        page.append(app_grid)

        app_grid.attach(Gtk.Label(label=_("Name:"), xalign=0.0), 0, 0, 1, 1)
        self._entry_name = Gtk.Entry()
        self._entry_name.set_hexpand(True)
        self._entry_name.set_placeholder_text(_("e.g. Quick terminal"))
        self._entry_name.connect("changed", self._on_form_edited)
        app_grid.attach(self._entry_name, 1, 0, 1, 1)

        app_grid.attach(Gtk.Label(label=_("Command:"), xalign=0.0), 0, 1, 1, 1)
        self._entry_exec = Gtk.Entry()
        self._entry_exec.set_hexpand(True)
        self._entry_exec.set_placeholder_text(_("e.g. alacritty or /path/to/binary"))
        self._entry_exec.connect("changed", self._on_exec_manual_changed)
        app_grid.attach(self._entry_exec, 1, 1, 1, 1)

        picker_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_menu_app = Gtk.Button(label=_("Installed app..."))
        btn_menu_app.connect("clicked", self._on_pick_desktop_app)
        btn_exec_file = Gtk.Button(label=_("Executable..."))
        btn_exec_file.connect("clicked", self._on_pick_executable_file)
        picker_row.append(btn_menu_app)
        picker_row.append(btn_exec_file)
        app_grid.attach(picker_row, 1, 2, 1, 1)

        app_grid.attach(Gtk.Label(label=_("Arguments:"), xalign=0.0), 0, 3, 1, 1)
        self._entry_extra_args = Gtk.Entry()
        self._entry_extra_args.set_hexpand(True)
        self._entry_extra_args.set_placeholder_text(_("Optional launch arguments"))
        self._entry_extra_args.connect("changed", self._on_form_edited)
        app_grid.attach(self._entry_extra_args, 1, 3, 1, 1)

        app_grid.attach(Gtk.Label(label=_("Window class:"), xalign=0.0), 0, 4, 1, 1)
        self._entry_match_hint = Gtk.Entry()
        self._entry_match_hint.set_hexpand(True)
        self._entry_match_hint.set_placeholder_text(_("WM_CLASS or app_id (optional)"))
        self._entry_match_hint.connect("changed", self._on_match_hint_changed)
        app_grid.attach(self._entry_match_hint, 1, 4, 1, 1)

        # Lifecycle mode with wrapped subtitle labels (zero horizontal overflow)
        page.append(self._section_label(_("Lifecycle")))
        mode_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        page.append(mode_box)

        self._radio_persistent = Gtk.CheckButton(label=LifecycleMode.PERSISTENT.label)
        self._radio_ephemeral = Gtk.CheckButton(label=LifecycleMode.EPHEMERAL.label)
        self._radio_tray = Gtk.CheckButton(label=LifecycleMode.TRAY.label)
        self._radio_ephemeral.set_group(self._radio_persistent)
        self._radio_tray.set_group(self._radio_persistent)
        self._radio_persistent.set_active(True)

        for radio, mode in (
            (self._radio_persistent, LifecycleMode.PERSISTENT),
            (self._radio_ephemeral, LifecycleMode.EPHEMERAL),
            (self._radio_tray, LifecycleMode.TRAY),
        ):
            radio.connect("toggled", self._on_form_edited)
            mode_box.append(radio)
            desc = Gtk.Label(label=mode.short_description, xalign=0.0)
            desc.set_wrap(True)
            desc.add_css_class("dim-label")
            desc.add_css_class("spawnkey-mode-desc")
            mode_box.append(desc)

        return page

    def _build_tab_geometry(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(6)
        page.set_margin_end(4)

        dim_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.append(dim_box)

        self._dd_unit = Gtk.DropDown.new_from_strings([_("Percent (%)"), _("Pixels (px)")])
        self._dd_unit.connect("notify::selected", self._on_unit_changed)
        dim_box.append(self._pair_box(_("Size unit:"), self._dd_unit))

        size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        size_row.set_halign(Gtk.Align.START)
        self._spin_width = Gtk.SpinButton.new_with_range(1, 99, 1)
        self._spin_width.set_value(80)
        self._spin_width.connect("value-changed", self._on_geometry_changed)
        self._spin_height = Gtk.SpinButton.new_with_range(1, 99, 1)
        self._spin_height.set_value(45)
        self._spin_height.connect("value-changed", self._on_geometry_changed)
        size_row.append(self._pair_box(_("Width:"), self._spin_width))
        size_row.append(self._pair_box(_("Height:"), self._spin_height))
        dim_box.append(size_row)

        self._dd_monitor_policy = Gtk.DropDown.new_from_strings(
            [_(lbl) for _policy, lbl in _MONITOR_POLICIES]
        )
        self._dd_monitor_policy.connect("notify::selected", self._on_monitor_policy_changed)
        dim_box.append(self._pair_box(_("Display:"), self._dd_monitor_policy))

        # Position mode switch: 3x3 preset vs custom
        page.append(self._section_label(_("Position")))
        pos_mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._radio_pos_preset = Gtk.CheckButton(label=_("3×3 grid"))
        self._radio_pos_custom = Gtk.CheckButton(label=_("Coordinates (X, Y)"))
        self._radio_pos_custom.set_group(self._radio_pos_preset)
        self._radio_pos_preset.set_active(True)
        self._radio_pos_preset.connect("toggled", self._on_position_mode_toggled)
        pos_mode_row.append(self._radio_pos_preset)
        pos_mode_row.append(self._radio_pos_custom)
        page.append(pos_mode_row)

        # Preset controls: compact 3x3 symbol grid + edge_margin
        self._preset_pos_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self._anchor_grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        self._anchor_grid.set_column_homogeneous(True)
        self._anchor_grid.set_row_homogeneous(True)
        for idx, (av, ah, symbol, tooltip) in enumerate(_ANCHOR_GRID):
            btn = Gtk.ToggleButton(label=symbol)
            btn.set_tooltip_text(_(tooltip))
            btn.set_size_request(40, 32)
            btn.connect("toggled", self._on_anchor_button_toggled, av, ah)
            self._anchor_buttons[(av, ah)] = btn
            self._anchor_grid.attach(btn, idx % 3, idx // 3, 1, 1)
        self._preset_pos_box.append(self._anchor_grid)

        margin_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        margin_box.set_valign(Gtk.Align.CENTER)
        margin_box.set_hexpand(True)
        self._spin_edge_margin = Gtk.SpinButton.new_with_range(0, 120, 2)
        self._spin_edge_margin.set_value(0)
        self._spin_edge_margin.connect("value-changed", self._on_geometry_changed)
        lbl_margin_hint = Gtk.Label(
            label=_("Offsets the window from the screen edge in edge-aligned positions."),
            xalign=0.0,
        )
        lbl_margin_hint.set_wrap(True)
        lbl_margin_hint.add_css_class("dim-label")
        lbl_margin_hint.add_css_class("caption")
        margin_box.append(self._pair_box(_("Edge margin (px):"), self._spin_edge_margin))
        margin_box.append(lbl_margin_hint)
        self._preset_pos_box.append(margin_box)
        page.append(self._preset_pos_box)

        # Custom X, Y coordinates with independent custom_position_unit
        self._custom_pos_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._dd_custom_unit = Gtk.DropDown.new_from_strings([_("Percent (%)"), _("Pixels (px)")])
        self._dd_custom_unit.connect("notify::selected", self._on_custom_unit_changed)
        self._custom_pos_box.append(self._pair_box(_("Position unit:"), self._dd_custom_unit))

        xy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        xy_row.set_halign(Gtk.Align.START)
        self._spin_custom_x = Gtk.SpinButton.new_with_range(0, 99, 1)
        self._spin_custom_x.connect("value-changed", self._on_geometry_changed)
        self._spin_custom_y = Gtk.SpinButton.new_with_range(0, 99, 1)
        self._spin_custom_y.connect("value-changed", self._on_geometry_changed)
        xy_row.append(self._pair_box(_("X:"), self._spin_custom_x))
        xy_row.append(self._pair_box(_("Y:"), self._spin_custom_y))
        self._custom_pos_box.append(xy_row)

        self._custom_pos_box.set_visible(False)
        page.append(self._custom_pos_box)

        return page

    def _build_tab_style(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(6)
        page.set_margin_end(4)

        page.append(self._section_label(_("Animation")))
        anim_select_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        anim_select_row.set_halign(Gtk.Align.START)

        self._dd_direction = Gtk.DropDown.new_from_strings([_(lbl) for _dir, lbl in _DIRECTIONS])
        self._dd_direction.connect("notify::selected", self._on_geometry_changed)
        anim_select_row.append(self._pair_box(_("Slide from:"), self._dd_direction))

        self._dd_animation = Gtk.DropDown.new_from_strings([_(lbl) for _anim, lbl in _ANIMATIONS])
        self._dd_animation.set_selected(2)  # slide-fade
        self._dd_animation.connect("notify::selected", self._on_animation_type_changed)
        anim_select_row.append(self._pair_box(_("Effect:"), self._dd_animation))
        page.append(anim_select_row)

        dur_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_dur = Gtk.Label(label=_("Duration:"), xalign=0.0)
        # Full model range: 0.1 to 5.0
        self._scale_anim = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.1, 5.0, 0.1)
        self._scale_anim.set_value(1.0)
        self._scale_anim.set_hexpand(True)
        self._scale_anim.set_draw_value(False)
        self._scale_anim.connect("value-changed", self._on_scale_changed)
        self._lbl_scale_value = Gtk.Label(label="220 ms (1.0×)", xalign=1.0)
        self._lbl_scale_value.set_width_chars(14)
        dur_row.append(lbl_dur)
        dur_row.append(self._scale_anim)
        dur_row.append(self._lbl_scale_value)
        page.append(dur_row)

        page.append(self._section_label(_("Appearance")))
        row_opacity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_opacity = Gtk.Label(label=_("Opacity:"), xalign=0.0)
        self._scale_opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.1, 1.0, 0.05)
        self._scale_opacity.set_value(1.0)
        self._scale_opacity.set_hexpand(True)
        self._scale_opacity.set_draw_value(False)
        self._scale_opacity.connect("value-changed", self._on_opacity_changed)
        self._lbl_opacity_value = Gtk.Label(label="100%", xalign=1.0)
        self._lbl_opacity_value.set_width_chars(5)
        row_opacity.append(lbl_opacity)
        row_opacity.append(self._scale_opacity)
        row_opacity.append(self._lbl_opacity_value)
        page.append(row_opacity)

        self._spin_corner_radius = Gtk.SpinButton.new_with_range(0, 50, 1)
        self._spin_corner_radius.set_value(0)
        self._spin_corner_radius.connect("value-changed", self._on_geometry_changed)
        page.append(self._pair_box(_("Rounded corners (px):"), self._spin_corner_radius))

        self._lbl_corner_hint = Gtk.Label(label="", xalign=0.0)
        self._lbl_corner_hint.set_wrap(True)
        self._lbl_corner_hint.add_css_class("dim-label")
        self._lbl_corner_hint.add_css_class("caption")
        page.append(self._lbl_corner_hint)

        lbl_border = Gtk.Label(
            label=_("No titlebar or system borders (always enabled)."),
            xalign=0.0,
        )
        lbl_border.set_wrap(True)
        lbl_border.add_css_class("dim-label")
        page.append(lbl_border)

        return page

    def _build_tab_behavior(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(6)
        page.set_margin_end(4)

        page.append(self._section_label(_("Behavior")))
        beh_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(beh_box)

        def _make_switch_row(text: str, active: bool) -> Gtk.Switch:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=text, xalign=0.0)
            lbl.set_hexpand(True)
            lbl.set_wrap(True)
            sw = Gtk.Switch()
            sw.set_active(active)
            sw.set_valign(Gtk.Align.CENTER)
            sw.connect("notify::active", self._on_form_edited)
            row.append(lbl)
            row.append(sw)
            beh_box.append(row)
            return sw

        self._sw_click_outside = _make_switch_row(
            _("Hide when clicking outside window"), True
        )
        self._sw_keep_above = _make_switch_row(
            _("Keep always on top of other windows"), True
        )
        self._sw_taskbar = _make_switch_row(
            _("Show in taskbar"), False
        )
        self._sw_relocate_on_toggle = _make_switch_row(
            _("Relocate/focus on current monitor instead of hiding"), True
        )
        self._sw_relocate_on_toggle.set_tooltip_text(
            _(
                "When monitor policy is cursor or focused, pressing the shortcut moves or focuses the open dropdown instead of hiding it."
            )
        )

        self._spin_startup_timeout = Gtk.SpinButton.new_with_range(1000, 60000, 500)
        self._spin_startup_timeout.set_value(8000)
        self._spin_startup_timeout.set_tooltip_text(
            _(
                "Maximum wait time to capture the new window on slow startups (Flatpak, Electron, Java)"
            )
        )
        self._spin_startup_timeout.connect("value-changed", self._on_form_edited)
        beh_box.append(self._pair_box(_("Startup timeout (ms):"), self._spin_startup_timeout))

        page.append(self._section_label(_("Global shortcut")))
        sc_widget, self._set_shortcut_text, self._get_shortcut_text = build_shortcut_capture_widget(
            self._on_shortcut_text_changed
        )
        page.append(sc_widget)

        self._lbl_conflict_warn = Gtk.Label(label="", xalign=0.0)
        self._lbl_conflict_warn.set_wrap(True)
        self._lbl_conflict_warn.add_css_class("spawnkey-conflict-warn")
        self._lbl_conflict_warn.set_visible(False)
        page.append(self._lbl_conflict_warn)

        self._sw_shortcut_enabled = _make_switch_row(_("Register shortcut on desktop"), True)
        self._sw_shortcut_enabled.connect("notify::active", lambda *_a: self._check_shortcut_conflicts())

        return page

    def _apply_backend_capabilities(self) -> None:
        if not self._manager.backend_available:
            self._lbl_corner_hint.set_text(_("Best-effort (no active backend detected)"))
            return

        caps = self._manager.capabilities
        backend_name = self._manager.env_info.backend.value

        self._scale_opacity.set_sensitive(caps.supports_opacity)
        if not caps.supports_opacity:
            self._scale_opacity.set_tooltip_text(
                _("The active backend ({backend}) does not support per-window opacity.").format(
                    backend=backend_name
                )
            )

        self._spin_corner_radius.set_sensitive(caps.supports_corner_radius)
        if caps.supports_corner_radius:
            self._lbl_corner_hint.set_text(_("Supported by {backend}").format(backend=backend_name))
            self._spin_corner_radius.set_tooltip_text(None)
        elif caps.corner_radius_hint:
            self._lbl_corner_hint.set_text(caps.corner_radius_hint)
            self._spin_corner_radius.set_tooltip_text(caps.corner_radius_hint)
        else:
            self._lbl_corner_hint.set_text(
                _("Not supported by the active backend ({backend})").format(backend=backend_name)
            )
            self._spin_corner_radius.set_tooltip_text(
                _("The active backend ({backend}) cannot enforce rounded corners.").format(
                    backend=backend_name
                )
            )

        self._sw_click_outside.set_sensitive(caps.supports_click_outside)
        if not caps.supports_click_outside:
            self._sw_click_outside.set_tooltip_text(
                _("The active backend ({backend}) does not support hiding on outside click.").format(
                    backend=backend_name
                )
            )

        if not caps.supports_animations:
            self._dd_direction.set_sensitive(False)
            self._dd_animation.set_sensitive(False)
            self._scale_anim.set_sensitive(False)

        if caps.backend_hint:
            if hasattr(self, "_status_label"):
                self._status_label.set_tooltip_text(caps.backend_hint)
            if not self._lbl_corner_hint.get_tooltip_text():
                self._lbl_corner_hint.set_tooltip_text(caps.backend_hint)

    # ------------------------------------------------------------------
    # State, dirty tracking & list synchronization
    # ------------------------------------------------------------------

    def _mark_dirty(self) -> None:
        if self._loading_form:
            return
        self._dirty = True

    def _on_form_edited(self, *_args) -> None:
        self._mark_dirty()

    def _confirm_discard_if_dirty(
        self,
        on_proceed: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        if not self._dirty:
            on_proceed()
            return

        dlg = Gtk.AlertDialog(
            message=_("Unsaved changes"),
            detail=_(
                "If you continue without saving, your current changes to this profile will be lost."
            ),
            buttons=[_("Cancel"), _("Discard changes")],
            cancel_button=0,
            default_button=0,
        )

        def _on_response(dialog: Gtk.AlertDialog, result: Gio.AsyncResult) -> None:
            try:
                choice = dialog.choose_finish(result)
            except Exception:
                choice = 0
            if choice == 1:
                self._dirty = False
                on_proceed()
            elif on_cancel is not None:
                on_cancel()

        dlg.choose(self, None, _on_response)

    def _check_shortcut_conflicts(self) -> None:
        shortcut = self._get_shortcut_text().strip()
        if not shortcut or not self._sw_shortcut_enabled.get_active():
            self._lbl_conflict_warn.set_visible(False)
            return
        conflicts = self._manager.find_shortcut_conflicts(
            shortcut,
            exclude_profile_id=self._current_id,
        )
        sys_conflicts = self._manager.find_system_shortcut_conflicts(shortcut)
        messages: list[str] = []
        if conflicts:
            names = ", ".join(c.name for c in conflicts)
            messages.append(_("Conflict with profile(s): {names}.").format(names=names))
        if sys_conflicts:
            messages.append(
                _("Desktop shortcut collision: {details}.").format(
                    details="; ".join(sys_conflicts)
                )
            )
        if messages:
            self._lbl_conflict_warn.set_text(" ".join(messages))
            self._lbl_conflict_warn.set_visible(True)
        else:
            self._lbl_conflict_warn.set_visible(False)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status_label.set_text(message)
        if error:
            self._status_label.remove_css_class("dim-label")
            self._status_label.add_css_class("error")
        else:
            self._status_label.remove_css_class("error")
            self._status_label.add_css_class("dim-label")

    def _reselect_current_row(self) -> None:
        self._suppress_row_select = True
        try:
            if self._current_id is None:
                self._list_box.unselect_all()
                return
            idx = 0
            while True:
                row = self._list_box.get_row_at_index(idx)
                if row is None:
                    break
                if getattr(row, "_profile_id", None) == self._current_id:
                    self._list_box.select_row(row)
                    break
                idx += 1
        finally:
            self._suppress_row_select = False

    def _refresh_list(self, *, select_id: str | None = None, select_first: bool = False) -> None:
        self._suppress_row_select = True
        try:
            while True:
                row = self._list_box.get_row_at_index(0)
                if row is None:
                    break
                self._list_box.remove(row)

            entries = self._manager.list_entries()
            target_row: Gtk.ListBoxRow | None = None
            target_profile: DropdownProfile | None = None

            for entry in entries:
                row = self._make_entry_row(entry)
                self._list_box.append(row)
                if select_id and entry.profile.id == select_id:
                    target_row = row
                    target_profile = entry.profile
                elif select_first and target_row is None:
                    target_row = row
                    target_profile = entry.profile

            if target_row is not None and target_profile is not None:
                self._list_box.select_row(target_row)
                self._load_profile_into_form(target_profile)
            elif not entries:
                self._load_new_blank_form()
        finally:
            self._suppress_row_select = False

        orphans = self._manager.list_orphaned_shortcuts()
        if orphans:
            self._orphan_label.set_text(
                ngettext(
                    "{count} orphaned shortcut",
                    "{count} orphaned shortcuts",
                    len(orphans),
                ).format(count=len(orphans))
            )
            self._orphan_box.set_visible(True)
        else:
            self._orphan_box.set_visible(False)

        self._update_list_buttons()

    def _make_entry_row(self, entry: ManagedEntry) -> Gtk.ListBoxRow:
        p = entry.profile
        row = Gtk.ListBoxRow()
        row._profile_id = p.id  # type: ignore[attr-defined]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_name = Gtk.Label(label=p.name, xalign=0.0)
        lbl_name.set_hexpand(True)
        lbl_name.set_ellipsize(Pango.EllipsizeMode.END)
        lbl_name.add_css_class("heading")
        mode_badges = {
            LifecycleMode.PERSISTENT: _("Persist."),
            LifecycleMode.EPHEMERAL: _("Ephemeral"),
            LifecycleMode.TRAY: _("Tray"),
        }
        lbl_mode = Gtk.Label(label=mode_badges[p.mode])
        lbl_mode.add_css_class("dim-label")
        lbl_mode.add_css_class("caption")
        top.append(lbl_name)
        top.append(lbl_mode)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        shortcut_text = p.shortcut or _("(no shortcut)")
        lbl_sc = Gtk.Label(label=shortcut_text, xalign=0.0)
        lbl_sc.set_hexpand(True)
        lbl_sc.set_ellipsize(Pango.EllipsizeMode.END)
        lbl_sc.add_css_class("dim-label")
        lbl_sc.add_css_class("caption")

        lbl_status = Gtk.Label(label=_(_STATUS_LABELS[entry.shortcut_status]))
        lbl_status.add_css_class("spawnkey-badge")
        lbl_status.add_css_class(_STATUS_CSS[entry.shortcut_status])

        bottom.append(lbl_sc)
        bottom.append(lbl_status)

        box.append(top)
        box.append(bottom)
        row.set_child(box)
        return row

    def _update_list_buttons(self) -> None:
        has_existing = (
            self._current_id is not None
            and self._manager.store.get_profile(self._current_id) is not None
        )
        has_any = self._list_box.get_row_at_index(0) is not None
        self._btn_list_test.set_sensitive(has_existing and self._manager.backend_available)
        self._btn_list_duplicate.set_sensitive(has_existing)
        self._btn_list_rm_shortcut.set_sensitive(has_existing)
        self._btn_list_delete.set_sensitive(has_existing)
        self._btn_export.set_sensitive(has_any)
        self._btn_test.set_sensitive(has_existing and self._manager.backend_available)

    def _load_new_blank_form(self, recipe: str = "blank") -> None:
        self._loading_form = True
        try:
            self._current_id = None
            self._current_source_type = SourceType.EXECUTABLE
            self._current_desktop_id = None
            self._current_match_hint = ""
            self._entry_name.set_text("")
            self._entry_exec.set_text("")
            self._entry_extra_args.set_text("")
            self._entry_match_hint.set_text("")
            self._radio_persistent.set_active(True)
            self._dd_unit.set_selected(0)
            self._configure_size_spin_ranges(SizeUnit.PERCENT)
            self._dd_custom_unit.set_selected(0)
            self._configure_custom_spin_ranges(SizeUnit.PERCENT)
            self._spin_width.set_value(80)
            self._spin_height.set_value(45)
            self._dd_monitor_policy.set_selected(0)
            self._radio_pos_preset.set_active(True)
            self._select_anchor(AnchorV.TOP, AnchorH.CENTER)
            self._spin_edge_margin.set_value(0)
            self._spin_custom_x.set_value(10)
            self._spin_custom_y.set_value(0)
            self._preset_pos_box.set_visible(True)
            self._custom_pos_box.set_visible(False)
            self._dd_direction.set_selected(0)
            self._dd_animation.set_selected(2)  # slide-fade
            self._scale_anim.set_value(1.0)
            self._sw_click_outside.set_active(True)
            self._sw_keep_above.set_active(True)
            self._sw_taskbar.set_active(False)
            self._sw_relocate_on_toggle.set_active(True)
            self._spin_startup_timeout.set_value(8000)
            self._scale_opacity.set_value(1.0)
            self._lbl_opacity_value.set_text("100%")
            self._spin_corner_radius.set_value(0)
            self._set_shortcut_text("")
            self._sw_shortcut_enabled.set_active(True)

            if recipe == "top_terminal":
                self._entry_name.set_text(_("Quick terminal"))
                self._radio_persistent.set_active(True)
                self._spin_width.set_value(85)
                self._spin_height.set_value(45)
                self._select_anchor(AnchorV.TOP, AnchorH.CENTER)
                self._dd_direction.set_selected(0)  # top
                self._dd_animation.set_selected(0)  # slide
            elif recipe == "tray_panel":
                self._entry_name.set_text(_("Side panel"))
                self._radio_tray.set_active(True)
                self._spin_width.set_value(38)
                self._spin_height.set_value(82)
                self._select_anchor(AnchorV.CENTER, AnchorH.RIGHT)
                self._spin_edge_margin.set_value(8)
                self._dd_direction.set_selected(3)  # right
                self._dd_animation.set_selected(2)  # slide-fade
            elif recipe == "ephemeral_center":
                self._entry_name.set_text(_("Quick utility"))
                self._radio_ephemeral.set_active(True)
                self._spin_width.set_value(55)
                self._spin_height.set_value(60)
                self._select_anchor(AnchorV.CENTER, AnchorH.CENTER)
                self._dd_animation.set_selected(1)  # fade
        finally:
            self._loading_form = False

        self._dirty = recipe != "blank"
        self._refresh_active_monitor(MonitorPolicy.CURSOR)
        self._check_shortcut_conflicts()
        self._update_animation_scale_state()
        self._update_preview()
        self._update_list_buttons()

    def _load_profile_into_form(self, profile: DropdownProfile, *, as_copy: bool = False) -> None:
        self._loading_form = True
        try:
            self._current_id = None if as_copy else profile.id
            self._current_source_type = profile.source_type
            self._current_desktop_id = profile.desktop_id
            self._current_match_hint = profile.match_hint

            self._entry_name.set_text(
                _("{name} (copy)").format(name=profile.name) if as_copy else profile.name
            )
            self._entry_exec.set_text(profile.exec_cmd)
            self._entry_extra_args.set_text(profile.extra_args)
            self._entry_match_hint.set_text(profile.match_hint)

            if profile.mode is LifecycleMode.PERSISTENT:
                self._radio_persistent.set_active(True)
            elif profile.mode is LifecycleMode.EPHEMERAL:
                self._radio_ephemeral.set_active(True)
            else:
                self._radio_tray.set_active(True)

            unit_idx = 0 if profile.size_unit is SizeUnit.PERCENT else 1
            self._dd_unit.set_selected(unit_idx)
            self._configure_size_spin_ranges(profile.size_unit)
            self._spin_width.set_value(profile.width)
            self._spin_height.set_value(profile.height)

            custom_unit_idx = 0 if profile.custom_position_unit is SizeUnit.PERCENT else 1
            self._dd_custom_unit.set_selected(custom_unit_idx)
            self._configure_custom_spin_ranges(profile.custom_position_unit)

            mon_idx = next(
                (i for i, (m, _) in enumerate(_MONITOR_POLICIES) if m is profile.monitor_policy),
                0,
            )
            self._dd_monitor_policy.set_selected(mon_idx)

            if profile.position_mode is PositionMode.PRESET:
                self._radio_pos_preset.set_active(True)
                self._preset_pos_box.set_visible(True)
                self._custom_pos_box.set_visible(False)
            else:
                self._radio_pos_custom.set_active(True)
                self._preset_pos_box.set_visible(False)
                self._custom_pos_box.set_visible(True)

            self._select_anchor(profile.anchor_v, profile.anchor_h)
            self._spin_edge_margin.set_value(profile.edge_margin)
            self._spin_custom_x.set_value(profile.custom_x)
            self._spin_custom_y.set_value(profile.custom_y)

            dir_idx = next((i for i, (d, _) in enumerate(_DIRECTIONS) if d is profile.direction), 0)
            self._dd_direction.set_selected(dir_idx)

            anim_idx = next((i for i, (a, _) in enumerate(_ANIMATIONS) if a is profile.animation_type), 2)
            self._dd_animation.set_selected(anim_idx)
            self._scale_anim.set_value(profile.animation_scale)

            self._sw_click_outside.set_active(profile.hide_on_click_outside)
            self._sw_keep_above.set_active(profile.keep_above)
            self._sw_taskbar.set_active(profile.show_in_taskbar)
            self._sw_relocate_on_toggle.set_active(profile.relocate_on_toggle)
            self._spin_startup_timeout.set_value(profile.startup_timeout_ms)
            self._scale_opacity.set_value(profile.opacity)
            self._lbl_opacity_value.set_text(f"{round(profile.opacity * 100)}%")
            self._spin_corner_radius.set_value(profile.corner_radius)
            self._set_shortcut_text("" if as_copy else profile.shortcut)
            self._sw_shortcut_enabled.set_active(False if as_copy else profile.shortcut_enabled)
        finally:
            self._loading_form = False

        self._dirty = as_copy
        self._refresh_active_monitor(profile.monitor_policy)
        self._check_shortcut_conflicts()
        self._update_animation_scale_state()
        self._update_preview()
        self._update_list_buttons()

    def _current_unit(self) -> SizeUnit:
        return SizeUnit.PERCENT if self._dd_unit.get_selected() == 0 else SizeUnit.PIXELS

    def _current_custom_unit(self) -> SizeUnit:
        return SizeUnit.PERCENT if self._dd_custom_unit.get_selected() == 0 else SizeUnit.PIXELS

    def _configure_size_spin_ranges(self, unit: SizeUnit) -> None:
        if unit is SizeUnit.PERCENT:
            self._spin_width.set_range(1, 99)
            self._spin_height.set_range(1, 99)
        else:
            max_w = max(50, self._monitor.width - 1)
            max_h = max(50, self._monitor.height - 1)
            self._spin_width.set_range(50, max_w)
            self._spin_height.set_range(50, max_h)

    def _configure_custom_spin_ranges(self, unit: SizeUnit) -> None:
        if unit is SizeUnit.PERCENT:
            self._spin_custom_x.set_range(0, 99)
            self._spin_custom_y.set_range(0, 99)
        else:
            max_w = max(50, self._monitor.width - 1)
            max_h = max(50, self._monitor.height - 1)
            self._spin_custom_x.set_range(0, max_w)
            self._spin_custom_y.set_range(0, max_h)

    def _select_anchor(self, av: AnchorV, ah: AnchorH) -> None:
        self._selected_anchor_v = av
        self._selected_anchor_h = ah
        prev_loading = self._loading_form
        self._loading_form = True
        try:
            for key, btn in self._anchor_buttons.items():
                btn.set_active(key == (av, ah))
        finally:
            self._loading_form = prev_loading

    def _build_draft_profile(self, *, strict: bool = False) -> DropdownProfile:
        name = self._entry_name.get_text().strip()
        exec_cmd = self._entry_exec.get_text().strip()
        if not strict:
            name = name or _("Preview")
            exec_cmd = exec_cmd or "true"

        profile_id = self._current_id or self._manager.store.ensure_unique_id(name or "dropdown")
        unit = self._current_unit()
        custom_unit = self._current_custom_unit()
        direction = _DIRECTIONS[self._dd_direction.get_selected()][0]
        anim_type = _ANIMATIONS[self._dd_animation.get_selected()][0]
        pos_mode = PositionMode.PRESET if self._radio_pos_preset.get_active() else PositionMode.CUSTOM
        if self._radio_persistent.get_active():
            mode = LifecycleMode.PERSISTENT
        elif self._radio_ephemeral.get_active():
            mode = LifecycleMode.EPHEMERAL
        else:
            mode = LifecycleMode.TRAY

        shortcut = self._get_shortcut_text()
        monitor_policy = _MONITOR_POLICIES[self._dd_monitor_policy.get_selected()][0]
        match_hint = self._entry_match_hint.get_text().strip() or self._current_match_hint
        extra_args = self._entry_extra_args.get_text().strip()
        return DropdownProfile(
            id=profile_id,
            name=name,
            exec_cmd=exec_cmd,
            source_type=self._current_source_type,
            desktop_id=self._current_desktop_id,
            match_hint=match_hint,
            extra_args=extra_args,
            mode=mode,
            startup_timeout_ms=int(self._spin_startup_timeout.get_value()),
            size_unit=unit,
            width=float(self._spin_width.get_value()),
            height=float(self._spin_height.get_value()),
            position_mode=pos_mode,
            anchor_h=self._selected_anchor_h,
            anchor_v=self._selected_anchor_v,
            edge_margin=int(self._spin_edge_margin.get_value()),
            custom_position_unit=custom_unit,
            custom_x=float(self._spin_custom_x.get_value()),
            custom_y=float(self._spin_custom_y.get_value()),
            monitor_policy=monitor_policy,
            relocate_on_toggle=self._sw_relocate_on_toggle.get_active(),
            borderless=True,
            hide_on_click_outside=self._sw_click_outside.get_active(),
            show_in_taskbar=self._sw_taskbar.get_active(),
            keep_above=self._sw_keep_above.get_active(),
            opacity=round(float(self._scale_opacity.get_value()), 2),
            corner_radius=int(self._spin_corner_radius.get_value()),
            direction=direction,
            animation_type=anim_type,
            animation_scale=round(float(self._scale_anim.get_value()), 2),
            shortcut=shortcut,
            shortcut_enabled=bool(shortcut and self._sw_shortcut_enabled.get_active()),
        )

    def _update_animation_scale_state(self) -> None:
        anim_type = _ANIMATIONS[self._dd_animation.get_selected()][0]
        enabled = anim_type is not AnimationType.NONE
        self._scale_anim.set_sensitive(enabled)
        if not enabled:
            self._lbl_scale_value.set_text(_("No animation"))
        else:
            scale = round(float(self._scale_anim.get_value()), 1)
            draft = self._build_draft_profile(strict=False)
            ms = compute_animation_duration_ms(draft)
            self._lbl_scale_value.set_text(f"{ms} ms ({scale:.1f}×)")

    def _resolve_gui_monitors(self, policy: MonitorPolicy) -> tuple[list[MonitorRect], MonitorRect]:
        surface = None
        if hasattr(self, "get_surface"):
            try:
                surface = self.get_surface()
            except Exception:
                surface = None
        monitors, primary_idx, focused_idx = detect_gdk_monitors(surface=surface)
        if monitors:
            active = select_monitor_by_policy(
                monitors,
                policy,
                primary_index=primary_idx,
                focused_index=focused_idx,
            )
            return monitors, active
        ref = DropdownProfile(id="ref", name="Ref", exec_cmd="true", monitor_policy=policy)
        fallback = self._manager.get_active_monitor(ref)
        return [fallback], fallback

    def _resolve_gui_monitor(self, policy: MonitorPolicy) -> MonitorRect:
        _monitors, active = self._resolve_gui_monitors(policy)
        return active

    def _refresh_active_monitor(self, policy: MonitorPolicy) -> None:
        monitors, new_mon = self._resolve_gui_monitors(policy)
        changed = (new_mon != self._monitor) or (monitors != self._monitors)
        self._monitors = monitors
        self._monitor = new_mon
        if changed and hasattr(self, "_preview"):
            self._preview.set_monitors(self._monitors, self._monitor)

    def _on_monitor_policy_changed(self, *_args) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        policy = _MONITOR_POLICIES[self._dd_monitor_policy.get_selected()][0]
        self._refresh_active_monitor(policy)
        self._update_preview()

    def _update_preview(self) -> None:
        try:
            draft = self._build_draft_profile(strict=False)
            self._preview.set_profile(draft)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_diagnostics_clicked(self, _btn: Gtk.Button) -> None:
        dialog = _DiagnosticsDialog(self, self._manager)
        dialog.present()

    def _on_export_clicked(self, _btn: Gtk.Button) -> None:
        selected_id = (
            self._current_id
            if (
                self._current_id is not None
                and self._manager.store.get_profile(self._current_id) is not None
            )
            else None
        )
        if selected_id is None:
            if not self._manager.store.list_profiles():
                self._set_status(_("No profiles to export."), error=True)
                return
            self._open_export_file_dialog(profile_ids=None)
            return

        dlg = Gtk.AlertDialog(
            message=_("Export profiles"),
            detail=_("Choose whether to export only the selected profile or all saved profiles."),
            buttons=[_("Cancel"), _("Export selected"), _("Export all")],
            cancel_button=0,
            default_button=2,
        )

        def _on_choice(dialog: Gtk.AlertDialog, result: Gio.AsyncResult) -> None:
            try:
                choice = dialog.choose_finish(result)
            except Exception:
                return
            if choice == 1:
                self._open_export_file_dialog(profile_ids=[selected_id])
            elif choice == 2:
                self._open_export_file_dialog(profile_ids=None)

        dlg.choose(self, None, _on_choice)

    def _open_export_file_dialog(self, profile_ids: list[str] | None) -> None:
        dialog = Gtk.FileDialog(title=_("Export profiles"))
        dialog.set_initial_name("spawnkey-profiles.json")
        filters, json_filter = _make_json_file_filters()
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter)

        def _on_save_done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = dlg.save_finish(result)
            except Exception:
                return
            if gfile is None:
                return
            path_str = gfile.get_path()
            if not path_str:
                return
            out_path = Path(path_str)
            try:
                self._manager.export_profiles_to_file(out_path, profile_ids=profile_ids)
            except Exception as exc:
                self._set_status(_("Export failed: {error}").format(error=exc), error=True)
                return
            self._set_status(_("Exported profiles to {path}.").format(path=out_path))

        dialog.save(self, None, _on_save_done)

    def _on_import_clicked(self, _btn: Gtk.Button) -> None:
        def _open_import() -> None:
            dialog = Gtk.FileDialog(title=_("Import profiles"))
            filters, json_filter = _make_json_file_filters()
            dialog.set_filters(filters)
            dialog.set_default_filter(json_filter)

            def _on_open_done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
                try:
                    gfile = dlg.open_finish(result)
                except Exception:
                    return
                if gfile is None:
                    return
                path_str = gfile.get_path()
                if not path_str:
                    return
                try:
                    res = self._manager.import_profiles_from_file(
                        Path(path_str),
                        overwrite=True,
                        register_shortcuts=True,
                    )
                except Exception as exc:
                    self._set_status(_("Import failed: {error}").format(error=exc), error=True)
                    return

                self._dirty = False
                first_id = (res.imported + res.updated + [None])[0]
                if first_id is not None:
                    self._refresh_list(select_id=first_id)
                else:
                    self._refresh_list(select_id=self._current_id)

                summary = _(
                    "Import complete: {imported} imported, {updated} updated, {skipped} skipped."
                ).format(
                    imported=len(res.imported),
                    updated=len(res.updated),
                    skipped=len(res.skipped),
                )
                if res.warnings:
                    warn_text = "; ".join(res.warnings)
                    full_msg = f"{summary} " + _("Warnings: {warnings}").format(warnings=warn_text)
                    self._set_status(full_msg, error=True)
                    self._status_label.set_tooltip_text(full_msg)
                else:
                    self._set_status(summary)
                    self._status_label.set_tooltip_text(summary)

            dialog.open(self, None, _on_open_done)

        self._confirm_discard_if_dirty(_open_import)

    def _on_row_selected(self, _lb: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._suppress_row_select or row is None:
            return
        pid: str = row._profile_id  # type: ignore[attr-defined]
        if pid == self._current_id:
            return

        def _load() -> None:
            profile = self._manager.store.get_profile(pid)
            if profile is not None:
                self._load_profile_into_form(profile)

        self._confirm_discard_if_dirty(_load, on_cancel=self._reselect_current_row)

    def _on_new_recipe_clicked(self, _btn: Gtk.Button, recipe: str) -> None:
        self._new_popover.popdown()

        def _start_new() -> None:
            self._suppress_row_select = True
            try:
                self._list_box.unselect_all()
            finally:
                self._suppress_row_select = False
            self._load_new_blank_form(recipe=recipe)
            self._stack.set_visible_child_name("app")
            self._entry_name.grab_focus()
            self._set_status(_("Creating new dropdown..."))

        self._confirm_discard_if_dirty(_start_new)

    def _on_duplicate_clicked(self, _btn: Gtk.Button) -> None:
        if not self._current_id:
            return
        source = self._manager.store.get_profile(self._current_id)
        if source is None:
            return

        def _do_duplicate() -> None:
            self._suppress_row_select = True
            try:
                self._list_box.unselect_all()
            finally:
                self._suppress_row_select = False
            self._load_profile_into_form(source, as_copy=True)
            self._stack.set_visible_child_name("app")
            self._entry_name.grab_focus()
            self._set_status(
                _("Duplicated '{name}'. Assign a shortcut and save.").format(name=source.name)
            )

        self._confirm_discard_if_dirty(_do_duplicate)

    def _on_exec_manual_changed(self, _entry: Gtk.Entry) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        if not self._entry_exec.get_text().strip():
            self._current_source_type = SourceType.EXECUTABLE
            self._current_desktop_id = None

    def _on_match_hint_changed(self, _entry: Gtk.Entry) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        self._current_match_hint = self._entry_match_hint.get_text().strip()

    def _on_opacity_changed(self, *_args) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        val = round(float(self._scale_opacity.get_value()), 2)
        self._lbl_opacity_value.set_text(f"{round(val * 100)}%")
        self._update_preview()

    def _on_pick_desktop_app(self, _btn: Gtk.Button) -> None:
        def _picked(app: DesktopApp) -> None:
            self._loading_form = True
            try:
                self._current_source_type = SourceType.DESKTOP
                self._current_desktop_id = app.desktop_id
                self._current_match_hint = app.wm_class
                if not self._entry_name.get_text().strip():
                    self._entry_name.set_text(app.name)
                resolved_cmd = wrap_terminal_command(app.exec_cmd) if app.terminal else app.exec_cmd
                self._entry_exec.set_text(resolved_cmd)
                self._entry_match_hint.set_text(app.wm_class)
            finally:
                self._loading_form = False
            self._mark_dirty()
            suffix = _(" [Terminal]") if app.terminal else ""
            self._set_status(
                _("Selected application: {name} ({desktop_id}){suffix}").format(
                    name=app.name,
                    desktop_id=app.desktop_id,
                    suffix=suffix,
                )
            )

        dialog = AppPickerDialog(self, _picked)
        dialog.present()

    def _on_pick_executable_file(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title=_("Select executable file"))

        def _on_done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = dlg.open_finish(result)
            except Exception:
                return
            if gfile is None:
                return
            path_str = gfile.get_path()
            if not path_str:
                return
            self._loading_form = True
            try:
                self._current_source_type = SourceType.EXECUTABLE
                self._current_desktop_id = None
                self._current_match_hint = Path(path_str).stem
                if not self._entry_name.get_text().strip():
                    self._entry_name.set_text(Path(path_str).stem.capitalize())
                self._entry_exec.set_text(path_str)
                self._entry_match_hint.set_text(self._current_match_hint)
            finally:
                self._loading_form = False
            self._mark_dirty()
            self._set_status(_("Selected executable: {path}").format(path=path_str))

        dialog.open(self, None, _on_done)

    def _on_unit_changed(self, _dd: Gtk.DropDown, _pspec) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        unit = self._current_unit()
        self._loading_form = True
        try:
            old_w = self._spin_width.get_value()
            old_h = self._spin_height.get_value()
            self._configure_size_spin_ranges(unit)
            if unit is SizeUnit.PIXELS:
                self._spin_width.set_value(min(self._monitor.width - 1, round(self._monitor.width * (old_w / 100.0))))
                self._spin_height.set_value(min(self._monitor.height - 1, round(self._monitor.height * (old_h / 100.0))))
            else:
                self._spin_width.set_value(max(1, min(99, round((old_w / self._monitor.width) * 100.0))))
                self._spin_height.set_value(max(1, min(99, round((old_h / self._monitor.height) * 100.0))))
        finally:
            self._loading_form = False
        self._update_preview()

    def _on_custom_unit_changed(self, _dd: Gtk.DropDown, _pspec) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        unit = self._current_custom_unit()
        self._loading_form = True
        try:
            old_cx = self._spin_custom_x.get_value()
            old_cy = self._spin_custom_y.get_value()
            self._configure_custom_spin_ranges(unit)
            if unit is SizeUnit.PIXELS:
                self._spin_custom_x.set_value(round(self._monitor.width * (old_cx / 100.0)))
                self._spin_custom_y.set_value(round(self._monitor.height * (old_cy / 100.0)))
            else:
                self._spin_custom_x.set_value(max(0, min(99, round((old_cx / self._monitor.width) * 100.0))))
                self._spin_custom_y.set_value(max(0, min(99, round((old_cy / self._monitor.height) * 100.0))))
        finally:
            self._loading_form = False
        self._update_preview()

    def _on_position_mode_toggled(self, _radio: Gtk.CheckButton) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        is_preset = self._radio_pos_preset.get_active()
        self._preset_pos_box.set_visible(is_preset)
        self._custom_pos_box.set_visible(not is_preset)
        self._update_preview()

    def _on_anchor_button_toggled(self, btn: Gtk.ToggleButton, av: AnchorV, ah: AnchorH) -> None:
        if self._loading_form:
            return
        if btn.get_active():
            self._mark_dirty()
            self._select_anchor(av, ah)
            self._update_preview()
        elif (av, ah) == (self._selected_anchor_v, self._selected_anchor_h):
            # Keep at least one anchor selected
            self._select_anchor(av, ah)

    def _on_preview_drag(self, x_val: float, y_val: float) -> None:
        self._loading_form = True
        try:
            self._spin_custom_x.set_value(x_val)
            self._spin_custom_y.set_value(y_val)
        finally:
            self._loading_form = False
        self._mark_dirty()
        self._update_preview()

    def _on_geometry_changed(self, *_args) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        self._update_preview()

    def _on_animation_type_changed(self, *_args) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        self._update_animation_scale_state()
        self._update_preview()

    def _on_scale_changed(self, *_args) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        self._update_animation_scale_state()
        self._update_preview()

    def _on_shortcut_text_changed(self, text: str) -> None:
        if self._loading_form:
            return
        self._mark_dirty()
        if text and not self._sw_shortcut_enabled.get_active():
            self._sw_shortcut_enabled.set_active(True)
        self._check_shortcut_conflicts()

    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        try:
            profile = self._build_draft_profile(strict=True)
            if profile.source_type is SourceType.EXECUTABLE:
                ok, detail = validate_executable_command(profile.exec_cmd)
                if not ok:
                    raise ValueError(detail)
            conflicts = (
                self._manager.find_shortcut_conflicts(
                    profile.shortcut, exclude_profile_id=profile.id
                )
                if (profile.shortcut and profile.shortcut_enabled)
                else []
            )
            saved, shortcut_msg = self._manager.save_profile(profile, sync_shortcut=True)
        except Exception as exc:
            self._set_status(_("Error: {error}").format(error=exc), error=True)
            return

        self._dirty = False
        self._current_id = saved.id
        self._refresh_list(select_id=saved.id)
        conflict_note = (
            _(" (Warning: shortcut shared with {names})").format(
                names=", ".join(c.name for c in conflicts)
            )
            if conflicts
            else ""
        )
        self._set_status(
            _("Saved '{name}'. {shortcut_msg}{conflict_note}")
            .format(
                name=saved.name,
                shortcut_msg=shortcut_msg,
                conflict_note=conflict_note,
            )
            .strip()
        )

    def _on_test_clicked(self, _btn: Gtk.Button) -> None:
        if not self._current_id:
            self._set_status(_("Save the dropdown first to test it."), error=True)
            return
        try:
            self._manager.test_profile(self._current_id)
            self._set_status(_("Toggled dropdown '{id}'.").format(id=self._current_id))
        except Exception as exc:
            self._set_status(_("Test failed: {error}").format(error=exc), error=True)

    def _on_remove_shortcut_only_clicked(self, _btn: Gtk.Button) -> None:
        if not self._current_id:
            return
        try:
            removed = self._manager.remove_shortcut_only(self._current_id, keep_keys=False)
        except Exception as exc:
            self._set_status(
                _("Failed to remove shortcut: {error}").format(error=exc),
                error=True,
            )
            return

        pid = self._current_id
        self._dirty = False
        self._refresh_list(select_id=pid)
        msg = (
            _("Shortcut removed from system.")
            if removed
            else _("Profile already had no registered shortcut.")
        )
        self._set_status(msg)

    def _on_delete_profile_clicked(self, _btn: Gtk.Button) -> None:
        if not self._current_id:
            return
        pid = self._current_id
        try:
            result = self._manager.delete_profile(pid)
        except Exception as exc:
            self._set_status(_("Delete failed: {error}").format(error=exc), error=True)
            return

        self._dirty = False
        self._current_id = None
        self._refresh_list(select_first=True)
        yes = _("yes")
        no = _("no")
        self._set_status(
            _(
                "Deleted '{id}' (profile={profile}, script={script}, shortcut={shortcut})."
            ).format(
                id=pid,
                profile=yes if result["profile"] else no,
                script=yes if result["script"] else no,
                shortcut=yes if result["shortcut"] else no,
            )
        )

    def _on_cleanup_orphans_clicked(self, _btn: Gtk.Button) -> None:
        cleaned = self._manager.cleanup_orphaned_shortcuts()
        self._refresh_list(select_id=self._current_id)
        self._set_status(
            ngettext(
                "Cleaned {count} orphaned shortcut.",
                "Cleaned {count} orphaned shortcuts.",
                len(cleaned),
            ).format(count=len(cleaned))
        )
