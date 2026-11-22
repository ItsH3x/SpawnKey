"""Monitor-aware geometry, clamping, positioning, and animation helpers."""

from __future__ import annotations

from spawnkey.core.models import (
    AnchorH,
    AnchorV,
    AnimationType,
    DropdownProfile,
    MonitorPolicy,
    MonitorRect,
    PositionMode,
    SizeUnit,
    SlideDirection,
    WindowGeometry,
)

BASE_ANIMATION_DURATION_MS = 220


def select_monitor_by_policy(
    monitors: list[MonitorRect],
    policy: MonitorPolicy,
    *,
    primary_index: int = 0,
    focused_index: int | None = None,
    cursor_pos: tuple[int, int] | None = None,
) -> MonitorRect:
    """Select the active monitor rectangle according to MonitorPolicy."""
    if not monitors:
        return MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    def _primary_monitor() -> MonitorRect:
        if 0 <= primary_index < len(monitors):
            return monitors[primary_index]
        for mon in monitors:
            if mon.x == 0 and mon.y == 0:
                return mon
        return monitors[0]

    def _monitor_at_cursor() -> MonitorRect | None:
        if cursor_pos is None:
            return None
        cx, cy = cursor_pos
        for mon in monitors:
            if mon.x <= cx < (mon.x + mon.width) and mon.y <= cy < (mon.y + mon.height):
                return mon
        return None

    if policy is MonitorPolicy.PRIMARY:
        return _primary_monitor()

    if policy is MonitorPolicy.CURSOR:
        by_cursor = _monitor_at_cursor()
        if by_cursor is not None:
            return by_cursor
        if focused_index is not None and 0 <= focused_index < len(monitors):
            return monitors[focused_index]
        return _primary_monitor()

    # MonitorPolicy.FOCUSED
    if focused_index is not None and 0 <= focused_index < len(monitors):
        return monitors[focused_index]
    by_cursor = _monitor_at_cursor()
    if by_cursor is not None:
        return by_cursor
    return _primary_monitor()


def clamp_dimension(value: int, monitor_limit: int, min_px: int = 50) -> int:
    """Ensure the dropdown dimension is strictly smaller than the active monitor."""
    max_allowed = max(1, monitor_limit - 1)
    effective_min = min(min_px, max_allowed)
    return max(effective_min, min(value, max_allowed))


def compute_geometry(profile: DropdownProfile, monitor: MonitorRect) -> WindowGeometry:
    """Resolve pixel size and coordinates for a profile on a given monitor."""
    if profile.size_unit is SizeUnit.PERCENT:
        raw_w = round(monitor.width * (profile.width / 100.0))
        raw_h = round(monitor.height * (profile.height / 100.0))
    else:
        raw_w = round(profile.width)
        raw_h = round(profile.height)

    width = clamp_dimension(raw_w, monitor.width)
    height = clamp_dimension(raw_h, monitor.height)

    if profile.position_mode is PositionMode.PRESET:
        margin = max(0, int(profile.edge_margin))
        if profile.anchor_h is AnchorH.LEFT:
            rel_x = margin
        elif profile.anchor_h is AnchorH.CENTER:
            rel_x = (monitor.width - width) // 2
        else:
            rel_x = monitor.width - width - margin

        if profile.anchor_v is AnchorV.TOP:
            rel_y = margin
        elif profile.anchor_v is AnchorV.CENTER:
            rel_y = (monitor.height - height) // 2
        else:
            rel_y = monitor.height - height - margin
    else:
        if profile.custom_position_unit is SizeUnit.PERCENT:
            rel_x = round(monitor.width * (profile.custom_x / 100.0))
            rel_y = round(monitor.height * (profile.custom_y / 100.0))
        else:
            rel_x = round(profile.custom_x)
            rel_y = round(profile.custom_y)

    # Clamp position so the entire window stays inside the monitor bounds.
    max_rel_x = max(0, monitor.width - width)
    max_rel_y = max(0, monitor.height - height)
    rel_x = max(0, min(rel_x, max_rel_x))
    rel_y = max(0, min(rel_y, max_rel_y))

    return WindowGeometry(
        x=monitor.x + rel_x,
        y=monitor.y + rel_y,
        width=width,
        height=height,
    )


def compute_slide_start(
    target: WindowGeometry,
    monitor: MonitorRect,
    direction: SlideDirection,
    *,
    allow_offscreen: bool = True,
) -> tuple[int, int]:
    """Compute starting (x, y) coordinates for slide / slide-fade entry animations."""
    if not allow_offscreen:
        # Fallback for compositors that clamp offscreen coordinates: start at monitor edge.
        if direction is SlideDirection.TOP:
            return (target.x, monitor.y)
        if direction is SlideDirection.BOTTOM:
            return (target.x, monitor.y + monitor.height - target.height)
        if direction is SlideDirection.LEFT:
            return (monitor.x, target.y)
        return (monitor.x + monitor.width - target.width, target.y)

    if direction is SlideDirection.TOP:
        return (target.x, monitor.y - target.height)
    if direction is SlideDirection.BOTTOM:
        return (target.x, monitor.y + monitor.height)
    if direction is SlideDirection.LEFT:
        return (monitor.x - target.width, target.y)
    return (monitor.x + monitor.width, target.y)


def compute_animation_duration_ms(profile: DropdownProfile) -> int:
    """Return animation duration in milliseconds (0 when animation is 'none')."""
    if profile.animation_type is AnimationType.NONE:
        return 0
    return max(30, round(BASE_ANIMATION_DURATION_MS * profile.animation_scale))
