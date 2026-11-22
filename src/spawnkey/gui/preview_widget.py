"""Interactive Cairo preview showing monitor bounds, dropdown placement, margin, and animation."""

from __future__ import annotations

import math
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from spawnkey.core.geometry import compute_animation_duration_ms, compute_geometry
from spawnkey.core.models import (
    AnimationType,
    DropdownProfile,
    MonitorRect,
    PositionMode,
    SizeUnit,
    SlideDirection,
)


class PreviewWidget(Gtk.Box):
    """Visual screen diagram that previews geometry, edge margin, and entry animations."""

    def __init__(
        self,
        monitor: MonitorRect,
        on_custom_drag: Callable[[float, float], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._monitor = monitor
        self._profile: DropdownProfile | None = None
        self._on_custom_drag = on_custom_drag
        self._screen_box = (0.0, 0.0, 1.0, 1.0)
        self._drag_origin = (0.0, 0.0)

        # Transient animation simulation state (zero daemon; runs only on button click)
        self._anim_source_id: int | None = None
        self._anim_progress: float = 1.0
        self._anim_elapsed_ms: float = 0.0
        self._anim_duration_ms: int = 0

        self._area = Gtk.DrawingArea()
        self._area.set_content_width(260)
        self._area.set_content_height(140)
        self._area.set_hexpand(True)
        self._area.set_draw_func(self._on_draw)

        frame = Gtk.Frame()
        frame.set_child(self._area)
        self.append(frame)

        info_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._info_label = Gtk.Label(label="", xalign=0.0)
        self._info_label.add_css_class("dim-label")
        self._info_label.set_wrap(True)
        self._info_label.set_hexpand(True)
        info_row.append(self._info_label)

        self._btn_simulate = Gtk.Button(label="Simular entrada")
        self._btn_simulate.set_tooltip_text("Simula la animación de entrada en la vista previa")
        self._btn_simulate.set_valign(Gtk.Align.CENTER)
        self._btn_simulate.connect("clicked", self._on_simulate_clicked)
        info_row.append(self._btn_simulate)
        self.append(info_row)

        # Allow clicking/dragging directly on preview when in custom position mode
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pointer_pressed)
        self._area.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._area.add_controller(drag)

        self.connect("unmap", self._on_unmap)

    def _stop_simulation(self) -> None:
        if self._anim_source_id is not None:
            GLib.source_remove(self._anim_source_id)
            self._anim_source_id = None
        self._anim_progress = 1.0

    def _on_unmap(self, *_args) -> None:
        self._stop_simulation()

    def set_monitor(self, monitor: MonitorRect) -> None:
        self._monitor = monitor
        self._area.queue_draw()

    def set_profile(self, profile: DropdownProfile) -> None:
        self._stop_simulation()
        self._profile = profile
        geom = compute_geometry(profile, self._monitor)
        hint = " · Arrastra para mover" if profile.position_mode is PositionMode.CUSTOM else ""
        extras: list[str] = []
        if profile.position_mode is PositionMode.PRESET and profile.edge_margin > 0:
            extras.append(f"Margen {profile.edge_margin}px")
        if profile.opacity < 1.0:
            extras.append(f"Opacidad {round(profile.opacity * 100)}%")
        if profile.corner_radius > 0:
            extras.append(f"Radio {profile.corner_radius}px")
        extra_str = (" · " + " · ".join(extras)) if extras else ""
        self._info_label.set_text(
            f"Monitor {self._monitor.width}×{self._monitor.height} · "
            f"Ventana {geom.width}×{geom.height} en ({geom.x - self._monitor.x}, {geom.y - self._monitor.y})"
            f"{extra_str}{hint}"
        )
        can_animate = profile.animation_type is not AnimationType.NONE
        self._btn_simulate.set_sensitive(can_animate)
        self._area.queue_draw()

    def _on_simulate_clicked(self, _btn: Gtk.Button) -> None:
        if self._profile is None or self._profile.animation_type is AnimationType.NONE:
            return
        self._stop_simulation()
        self._anim_duration_ms = max(60, compute_animation_duration_ms(self._profile))
        self._anim_elapsed_ms = 0.0
        self._anim_progress = 0.0
        self._area.queue_draw()
        self._anim_source_id = GLib.timeout_add(16, self._on_anim_tick)

    def _on_anim_tick(self) -> bool:
        self._anim_elapsed_ms += 16.0
        raw_t = min(1.0, self._anim_elapsed_ms / max(1, self._anim_duration_ms))
        self._anim_progress = 1.0 - math.pow(1.0 - raw_t, 3)
        self._area.queue_draw()
        if raw_t >= 1.0:
            self._anim_source_id = None
            self._anim_progress = 1.0
            return False
        return True

    def _pointer_to_custom_coords(self, px: float, py: float) -> None:
        if not self._profile or self._profile.position_mode is not PositionMode.CUSTOM:
            return
        if not self._on_custom_drag:
            return

        sx, sy, sw, sh = self._screen_box
        if sw <= 1 or sh <= 1:
            return

        geom = compute_geometry(self._profile, self._monitor)
        # Treat pointer as center of the dropdown for natural dragging
        norm_x = (px - sx) / sw
        norm_y = (py - sy) / sh
        rel_px_x = round(norm_x * self._monitor.width - geom.width / 2)
        rel_px_y = round(norm_y * self._monitor.height - geom.height / 2)

        rel_px_x = max(0, min(rel_px_x, max(0, self._monitor.width - geom.width)))
        rel_px_y = max(0, min(rel_px_y, max(0, self._monitor.height - geom.height)))

        if self._profile.custom_position_unit is SizeUnit.PERCENT:
            val_x = round((rel_px_x / self._monitor.width) * 100.0, 1)
            val_y = round((rel_px_y / self._monitor.height) * 100.0, 1)
            val_x = max(0.0, min(99.0, val_x))
            val_y = max(0.0, min(99.0, val_y))
            self._on_custom_drag(val_x, val_y)
        else:
            self._on_custom_drag(float(rel_px_x), float(rel_px_y))

    def _on_pointer_pressed(self, _gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        self._pointer_to_custom_coords(x, y)

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_origin = (x, y)
        self._pointer_to_custom_coords(x, y)

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        ox, oy = self._drag_origin
        self._pointer_to_custom_coords(ox + dx, oy + dy)

    def _on_draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        pad = 10.0
        avail_w = max(10.0, width - 2 * pad)
        avail_h = max(10.0, height - 2 * pad)
        aspect = self._monitor.width / max(1, self._monitor.height)

        if avail_w / avail_h > aspect:
            sh = avail_h
            sw = sh * aspect
        else:
            sw = avail_w
            sh = sw / aspect

        sx = (width - sw) / 2.0
        sy = (height - sh) / 2.0
        self._screen_box = (sx, sy, sw, sh)

        # Background
        cr.set_source_rgb(0.11, 0.12, 0.15)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Monitor frame
        cr.set_source_rgb(0.18, 0.20, 0.25)
        cr.rectangle(sx, sy, sw, sh)
        cr.fill()
        cr.set_source_rgb(0.45, 0.50, 0.60)
        cr.set_line_width(1.5)
        cr.rectangle(sx, sy, sw, sh)
        cr.stroke()

        # Subtle 3x3 guide lines
        cr.set_source_rgba(0.6, 0.65, 0.75, 0.18)
        cr.set_line_width(1.0)
        for frac in (1.0 / 3.0, 2.0 / 3.0):
            cr.move_to(sx + sw * frac, sy)
            cr.line_to(sx + sw * frac, sy + sh)
            cr.move_to(sx, sy + sh * frac)
            cr.line_to(sx + sw, sy + sh * frac)
        cr.stroke()

        if self._profile is None:
            return

        geom = compute_geometry(self._profile, self._monitor)
        scale_x = sw / self._monitor.width
        scale_y = sh / self._monitor.height

        target_wx = sx + (geom.x - self._monitor.x) * scale_x
        target_wy = sy + (geom.y - self._monitor.y) * scale_y
        ww = max(6.0, geom.width * scale_x)
        wh = max(6.0, geom.height * scale_y)

        # Apply live animation interpolation when simulating
        t = self._anim_progress
        do_slide = self._profile.animation_type in (AnimationType.SLIDE, AnimationType.SLIDE_FADE)
        do_fade = self._profile.animation_type in (AnimationType.FADE, AnimationType.SLIDE_FADE)

        wx, wy = target_wx, target_wy
        if do_slide and t < 1.0:
            step_x = min(ww, max(18.0, sw * 0.35))
            step_y = min(wh, max(18.0, sh * 0.35))
            d = self._profile.direction
            if d is SlideDirection.TOP:
                start_y = sy - step_y
                wy = start_y + (target_wy - start_y) * t
            elif d is SlideDirection.BOTTOM:
                start_y = sy + sh - wh + step_y
                wy = start_y + (target_wy - start_y) * t
            elif d is SlideDirection.LEFT:
                start_x = sx - step_x
                wx = start_x + (target_wx - start_x) * t
            else:
                start_x = sx + sw - ww + step_x
                wx = start_x + (target_wx - start_x) * t

        # Clip dropdown inside monitor frame during slide simulation
        cr.save()
        cr.rectangle(sx, sy, sw, sh)
        cr.clip()

        # Dropdown window rectangle (no titlebar!), honoring opacity & corner_radius
        base_opacity = max(0.1, min(1.0, float(self._profile.opacity)))
        eff_opacity = base_opacity * (t if do_fade else 1.0)
        raw_radius = max(0, int(self._profile.corner_radius))
        if raw_radius > 0:
            r = max(2.0, min(ww / 2.0, wh / 2.0, raw_radius * min(scale_x, scale_y)))
        else:
            r = 0.0

        def _rounded_rect_path() -> None:
            if r <= 0.0:
                cr.rectangle(wx, wy, ww, wh)
                return
            cr.new_sub_path()
            cr.arc(wx + ww - r, wy + r, r, -math.pi / 2.0, 0.0)
            cr.arc(wx + ww - r, wy + wh - r, r, 0.0, math.pi / 2.0)
            cr.arc(wx + r, wy + wh - r, r, math.pi / 2.0, math.pi)
            cr.arc(wx + r, wy + r, r, math.pi, 1.5 * math.pi)
            cr.close_path()

        cr.set_source_rgba(0.24, 0.62, 0.96, (0.10 + 0.55 * base_opacity) * (t if do_fade else 1.0))
        _rounded_rect_path()
        cr.fill()
        cr.set_source_rgba(0.38, 0.75, 1.0, (0.35 + 0.65 * base_opacity) * (t if do_fade else 1.0))
        cr.set_line_width(2.0)
        _rounded_rect_path()
        cr.stroke()

        # Direction arrow (if slide or slide-fade and not mid-fade-in near 0)
        if do_slide and eff_opacity > 0.15:
            cx = wx + ww / 2.0
            cy = wy + wh / 2.0
            d = self._profile.direction
            arrow_len = min(22.0, max(10.0, min(ww, wh) * 0.42))

            if d is SlideDirection.TOP:
                x1, y1, x2, y2 = cx, wy + 4.0, cx, wy + 4.0 + arrow_len
            elif d is SlideDirection.BOTTOM:
                x1, y1, x2, y2 = cx, wy + wh - 4.0, cx, wy + wh - 4.0 - arrow_len
            elif d is SlideDirection.LEFT:
                x1, y1, x2, y2 = wx + 4.0, cy, wx + 4.0 + arrow_len, cy
            else:
                x1, y1, x2, y2 = wx + ww - 4.0, cy, wx + ww - 4.0 - arrow_len, cy

            cr.set_source_rgba(1.0, 0.85, 0.35, min(1.0, eff_opacity + 0.2))
            cr.set_line_width(2.0)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()

            angle = math.atan2(y2 - y1, x2 - x1)
            head = 5.5
            cr.move_to(x2, y2)
            cr.line_to(x2 - head * math.cos(angle - 0.5), y2 - head * math.sin(angle - 0.5))
            cr.move_to(x2, y2)
            cr.line_to(x2 - head * math.cos(angle + 0.5), y2 - head * math.sin(angle + 0.5))
            cr.stroke()

        cr.restore()
