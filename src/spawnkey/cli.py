"""Command-line interface for SpawnKey profiles, runner, and shortcut manager."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

from spawnkey.backends.base import BackendError
from spawnkey.backends.factory import UnsupportedBackendError, get_backends
from spawnkey.core.storage import (
    DEFAULT_RUN_DEBOUNCE_MS,
    AppPaths,
    ProfileStore,
    RuntimeStateStore,
    profile_run_guard,
)
from spawnkey.i18n import _, ngettext


def _parse_monitor(spec: str):
    from spawnkey.core.models import MonitorRect

    parts = spec.lower().split("x", 1)
    if len(parts) != 2:
        raise ValueError(
            _("Invalid monitor format '{spec}', use WIDTHxHEIGHT (e.g. 1920x1080).").format(
                spec=spec
            )
        )
    return MonitorRect(x=0, y=0, width=int(parts[0]), height=int(parts[1]), name="preview")


def _format_profile_position(profile) -> str:
    from spawnkey.core.models import PositionMode

    if profile.position_mode is PositionMode.PRESET:
        margin_str = (
            _(" (margin {px}px)").format(px=profile.edge_margin)
            if profile.edge_margin > 0
            else ""
        )
        return _("anchor {v}-{h}{margin}").format(
            v=profile.anchor_v.value,
            h=profile.anchor_h.value,
            margin=margin_str,
        )
    return _("custom ({x}, {y} {unit})").format(
        x=profile.custom_x,
        y=profile.custom_y,
        unit=profile.custom_position_unit.value,
    )


def _format_profile_animation(profile) -> str:
    if not profile.animation_scale_enabled:
        return _("none (scale disabled)")
    return _("{anim} from {direction} (x{scale})").format(
        anim=profile.animation_type.value,
        direction=profile.direction.value,
        scale=profile.animation_scale,
    )


def _cmd_info(_args: argparse.Namespace) -> int:
    from spawnkey.backends.detector import detect_environment

    info = detect_environment()
    paths = AppPaths.from_env()
    available = [name for name, ok in sorted(info.tools.items()) if ok]
    yes = _("yes")
    no = _("no")

    print(_("Graphics session : {value}").format(value=info.session_type))
    print(_("Desktop          : {value}").format(value=info.desktop))
    print(_("Backend          : {value}").format(value=info.backend.value))
    try:
        wb, _sb = get_backends(paths, env_info=info)
        caps = wb.capabilities
        print(
            _(
                "Capabilities     : opacity={opacity} | corner_radius={corner_radius} | "
                "click_outside={click_outside} | animations={animations}"
            ).format(
                opacity=yes if caps.supports_opacity else no,
                corner_radius=yes if caps.supports_corner_radius else no,
                click_outside=yes if caps.supports_click_outside else no,
                animations=yes if caps.supports_animations else no,
            )
        )
        if caps.corner_radius_hint:
            print(_("Radius notice    : {value}").format(value=caps.corner_radius_hint))
        if caps.backend_hint:
            print(_("Backend notice   : {value}").format(value=caps.backend_hint))
    except UnsupportedBackendError:
        pass
    print(
        _("Tools            : {value}").format(
            value=", ".join(available) if available else _("(none detected)")
        )
    )
    print(_("Profiles (XDG)   : {value}").format(value=paths.profiles_dir))
    print(_("Scripts  (XDG)   : {value}").format(value=paths.scripts_dir))
    print(_("State    (XDG)   : {value}").format(value=paths.state_dir))
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from spawnkey.core.manager import SpawnKeyManager

    manager = SpawnKeyManager(AppPaths.from_env())
    items = manager.diagnose()
    labels = {"ok": _("OK"), "warn": _("WARN"), "error": _("ERROR")}
    counts = {"ok": 0, "warn": 0, "error": 0}

    for level, check, detail in items:
        norm = level.lower()
        if norm not in counts:
            norm = "warn"
        counts[norm] += 1
        print(f"[{labels[norm]}] {check}: {detail}")

    print(
        "\n"
        + _("Summary: {ok} OK, {warn} warning(s), {error} error(s).").format(
            ok=counts["ok"],
            warn=counts["warn"],
            error=counts["error"],
        )
    )
    return 1 if counts["error"] > 0 else 0


def _cmd_apps(args: argparse.Namespace) -> int:
    from spawnkey.core.desktop_apps import discover_desktop_apps

    apps = discover_desktop_apps(query=args.query)
    total = len(apps)
    shown = apps[: args.limit] if args.limit > 0 else apps
    suffix = (
        _(" (showing {count})").format(count=len(shown))
        if len(shown) < total
        else ""
    )
    print(_("Applications found: {total}{suffix}").format(total=total, suffix=suffix))
    for app in shown:
        term_tag = _(" [Terminal]") if app.terminal else ""
        print(f"  - {app.name}{term_tag} [{app.desktop_id}] -> {app.exec_cmd}")
    return 0


def _cmd_profile_list(_args: argparse.Namespace) -> int:
    store = ProfileStore()
    profiles = store.list_profiles()
    if not profiles:
        print(_("No profiles configured yet."))
        return 0

    shortcut_backend = None
    try:
        _wb, shortcut_backend = get_backends(store.paths)
    except UnsupportedBackendError:
        pass

    yes = _("yes")
    no = _("no")

    for p in profiles:
        pos = _format_profile_position(p)
        anim = _format_profile_animation(p)
        status_str = ""
        if shortcut_backend is not None:
            status = shortcut_backend.get_shortcut_status(p)
            status_str = f" [{status.value}]"
        extra_str = f" {p.extra_args}" if p.extra_args else ""
        print(f"- {p.id}: {p.name}")
        print("    " + _("Command   : {cmd}").format(cmd=f"{p.exec_cmd}{extra_str}"))
        print(
            "    "
            + _("Mode      : {label} — {desc}").format(
                label=p.mode.label,
                desc=p.mode.short_description,
            )
        )
        print(
            "    "
            + _("Size      : {w}x{h} ({unit}) | Position: {pos} | Monitor: {mon}").format(
                w=p.width,
                h=p.height,
                unit=p.size_unit.value,
                pos=pos,
                mon=p.monitor_policy.value,
            )
        )
        print("    " + _("Animation : {anim}").format(anim=anim))
        print(
            "    "
            + _(
                "Options   : borderless={yes} | keep_above={above} | "
                "click_outside={outside} | taskbar={taskbar} | "
                "relocate_on_toggle={relocate} | opacity={opacity}% | "
                "corner_radius={radius}px | timeout={timeout}ms"
            ).format(
                yes=yes,
                above=yes if p.keep_above else no,
                outside=yes if p.hide_on_click_outside else no,
                taskbar=yes if p.show_in_taskbar else no,
                relocate=yes if p.relocate_on_toggle else no,
                opacity=round(p.opacity * 100),
                radius=p.corner_radius,
                timeout=p.startup_timeout_ms,
            )
        )
        print(
            "    "
            + _("Shortcut  : {shortcut}{status} | Script: {script}").format(
                shortcut=p.shortcut or _("(unassigned)"),
                status=status_str,
                script=p.script_path or _("(not generated)"),
            )
        )
    return 0


def _cmd_profile_show(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = store.get_profile(args.id)
    if not profile:
        print(
            _("Error: profile '{id}' not found.").format(id=args.id),
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        return 0

    shortcut_backend = None
    try:
        _wb, shortcut_backend = get_backends(store.paths)
    except UnsupportedBackendError:
        pass

    yes = _("yes")
    no = _("no")
    status_str = ""
    if shortcut_backend is not None:
        status_str = f" [{shortcut_backend.get_shortcut_status(profile).value}]"

    print(_("Profile ID        : {value}").format(value=profile.id))
    print(_("Name              : {value}").format(value=profile.name))
    print(_("Source type       : {value}").format(value=profile.source_type.value))
    if profile.desktop_id:
        print(_("Desktop ID        : {value}").format(value=profile.desktop_id))
    print(_("Command           : {value}").format(value=profile.exec_cmd))
    print(_("Extra args        : {value}").format(value=profile.extra_args or _("(none)")))
    print(_("Match hint        : {value}").format(value=profile.match_hint or _("(none)")))
    print(
        _("Mode              : {label} ({value}) — {desc}").format(
            label=profile.mode.label,
            value=profile.mode.value,
            desc=profile.mode.short_description,
        )
    )
    print(_("Startup timeout   : {ms}ms").format(ms=profile.startup_timeout_ms))
    print(
        _("Size              : {w}x{h} ({unit})").format(
            w=profile.width,
            h=profile.height,
            unit=profile.size_unit.value,
        )
    )
    print(_("Position          : {value}").format(value=_format_profile_position(profile)))
    print(_("Monitor policy    : {value}").format(value=profile.monitor_policy.value))
    print(
        _("Relocate on toggle: {value}").format(
            value=yes if profile.relocate_on_toggle else no
        )
    )
    print(_("Animation         : {value}").format(value=_format_profile_animation(profile)))
    print(
        _(
            "Options           : borderless={yes} | keep_above={above} | "
            "click_outside={outside} | taskbar={taskbar} | "
            "opacity={opacity}% | corner_radius={radius}px"
        ).format(
            yes=yes,
            above=yes if profile.keep_above else no,
            outside=yes if profile.hide_on_click_outside else no,
            taskbar=yes if profile.show_in_taskbar else no,
            opacity=round(profile.opacity * 100),
            radius=profile.corner_radius,
        )
    )
    print(
        _("Shortcut          : {shortcut}{status} (enabled={enabled})").format(
            shortcut=profile.shortcut or _("(unassigned)"),
            status=status_str,
            enabled=yes if profile.shortcut_enabled else no,
        )
    )
    print(
        _("Script path       : {value}").format(
            value=profile.script_path or _("(not generated)")
        )
    )
    return 0


def _cmd_profile_create(args: argparse.Namespace) -> int:
    from spawnkey.core.desktop_apps import (
        discover_desktop_apps,
        validate_executable_command,
        wrap_terminal_command,
    )
    from spawnkey.core.models import (
        AnchorH,
        AnchorV,
        AnimationType,
        DropdownProfile,
        LifecycleMode,
        MonitorPolicy,
        PositionMode,
        SizeUnit,
        SlideDirection,
        SourceType,
    )
    from spawnkey.core.script_gen import ScriptGenerator
    from spawnkey.core.shortcuts import find_profile_shortcut_conflicts

    store = ProfileStore()
    gen = ScriptGenerator(store.paths)

    source_type = SourceType.EXECUTABLE
    desktop_id = None
    match_hint = args.match_hint or ""
    exec_cmd = args.exec_cmd or ""
    name = args.name or ""

    if args.desktop:
        apps = discover_desktop_apps()
        match = next(
            (
                a
                for a in apps
                if a.desktop_id == args.desktop or a.desktop_id == f"{args.desktop}.desktop"
            ),
            None,
        )
        if not match:
            print(
                _("Error: .desktop app '{desktop}' not found.").format(
                    desktop=args.desktop
                ),
                file=sys.stderr,
            )
            return 1
        source_type = SourceType.DESKTOP
        desktop_id = match.desktop_id
        exec_cmd = wrap_terminal_command(match.exec_cmd) if match.terminal else match.exec_cmd
        name = name or match.name
        match_hint = match_hint or match.wm_class
    else:
        ok, detail = validate_executable_command(exec_cmd)
        if not ok:
            print(_("Executable error: {detail}").format(detail=detail), file=sys.stderr)
            return 1
        name = name or exec_cmd.split()[0]

    profile_id = args.id or store.ensure_unique_id(name)
    custom_unit = SizeUnit(args.custom_unit) if args.custom_unit else SizeUnit(args.unit)
    profile = DropdownProfile(
        id=profile_id,
        name=name,
        exec_cmd=exec_cmd,
        source_type=source_type,
        desktop_id=desktop_id,
        match_hint=match_hint,
        extra_args=args.extra_args or "",
        mode=LifecycleMode(args.mode),
        startup_timeout_ms=args.startup_timeout_ms,
        size_unit=SizeUnit(args.unit),
        width=args.width,
        height=args.height,
        position_mode=PositionMode(args.position_mode),
        anchor_h=AnchorH(args.anchor_h),
        anchor_v=AnchorV(args.anchor_v),
        edge_margin=args.edge_margin,
        custom_position_unit=custom_unit,
        custom_x=args.custom_x,
        custom_y=args.custom_y,
        monitor_policy=MonitorPolicy(args.monitor_policy),
        relocate_on_toggle=not args.no_relocate_on_toggle,
        hide_on_click_outside=not args.no_click_outside,
        show_in_taskbar=args.show_in_taskbar,
        keep_above=not args.no_keep_above,
        opacity=args.opacity,
        corner_radius=args.corner_radius,
        direction=SlideDirection(args.direction),
        animation_type=AnimationType(args.animation),
        animation_scale=args.scale,
        shortcut=args.shortcut or "",
        shortcut_enabled=bool(args.shortcut and not args.no_register),
    )

    if profile.shortcut and profile.shortcut_enabled:
        conflicts = find_profile_shortcut_conflicts(
            store.list_profiles(),
            profile.shortcut,
            exclude_profile_id=profile.id,
        )
        if conflicts:
            names = ", ".join(f"{c.name} ({c.id})" for c in conflicts)
            print(
                _("Warning       : shortcut '{shortcut}' is already assigned to: {names}.").format(
                    shortcut=profile.shortcut,
                    names=names,
                ),
                file=sys.stderr,
            )

    script_path = gen.generate(profile)
    registered_msg = _("(no shortcut)")
    active_window_backend = None

    try:
        active_window_backend, shortcut_backend = get_backends(store.paths)
    except UnsupportedBackendError as exc:
        shortcut_backend = None
        backend_err = exc
    else:
        backend_err = None

    if profile.shortcut and not args.no_register:
        if shortcut_backend is None:
            profile.shortcut_enabled = False
            registered_msg = _("{shortcut} (saved, but not registered: {error})").format(
                shortcut=profile.shortcut,
                error=backend_err,
            )
        else:
            try:
                shortcut_backend.register_shortcut(profile)
                registered_msg = _("{shortcut} (registered in {backend})").format(
                    shortcut=profile.shortcut,
                    backend=shortcut_backend.name,
                )
            except BackendError as exc:
                profile.shortcut_enabled = False
                registered_msg = _("{shortcut} (saved, but not registered: {error})").format(
                    shortcut=profile.shortcut,
                    error=exc,
                )

    profile_path = store.save_profile(profile)
    print(_("Profile created : {id} -> {path}").format(id=profile.id, path=profile_path))
    print(_("Script created  : {path}").format(path=script_path))
    print(_("Shortcut        : {status}").format(status=registered_msg))
    if (
        profile.corner_radius > 0
        and active_window_backend is not None
        and not active_window_backend.capabilities.supports_corner_radius
    ):
        hint = active_window_backend.capabilities.corner_radius_hint
        detail = f" ({hint})" if hint else ""
        print(
            _(
                "Warning         : active backend ({backend}) does not support rounded corners{detail}; "
                "value ({radius}px) was saved for compatible compositors."
            ).format(
                backend=active_window_backend.name,
                detail=detail,
                radius=profile.corner_radius,
            ),
            file=sys.stderr,
        )
    return 0


def _cmd_profile_edit(args: argparse.Namespace) -> int:
    from spawnkey.core.desktop_apps import (
        discover_desktop_apps,
        validate_executable_command,
        wrap_terminal_command,
    )
    from spawnkey.core.models import DropdownProfile, SourceType, infer_match_hint_from_cmd
    from spawnkey.core.script_gen import ScriptGenerator
    from spawnkey.core.shortcuts import find_profile_shortcut_conflicts

    store = ProfileStore()
    profile = store.get_profile(args.id)
    if not profile:
        print(
            _("Error: profile '{id}' not found.").format(id=args.id),
            file=sys.stderr,
        )
        return 1

    old_shortcut = profile.shortcut
    old_enabled = profile.shortcut_enabled
    data = profile.to_dict()

    if args.desktop is not None:
        apps = discover_desktop_apps()
        match = next(
            (
                a
                for a in apps
                if a.desktop_id == args.desktop or a.desktop_id == f"{args.desktop}.desktop"
            ),
            None,
        )
        if not match:
            print(
                _("Error: .desktop app '{desktop}' not found.").format(
                    desktop=args.desktop
                ),
                file=sys.stderr,
            )
            return 1
        data["source_type"] = SourceType.DESKTOP.value
        data["desktop_id"] = match.desktop_id
        data["exec_cmd"] = (
            wrap_terminal_command(match.exec_cmd) if match.terminal else match.exec_cmd
        )
        if args.match_hint is None:
            data["match_hint"] = match.wm_class
    elif args.exec_cmd is not None:
        ok, detail = validate_executable_command(args.exec_cmd)
        if not ok:
            print(_("Executable error: {detail}").format(detail=detail), file=sys.stderr)
            return 1
        data["source_type"] = SourceType.EXECUTABLE.value
        data["desktop_id"] = None
        data["exec_cmd"] = args.exec_cmd
        if args.match_hint is None:
            data["match_hint"] = infer_match_hint_from_cmd(args.exec_cmd)

    direct_updates = {
        "name": args.name,
        "match_hint": args.match_hint,
        "extra_args": args.extra_args,
        "mode": args.mode,
        "startup_timeout_ms": args.startup_timeout_ms,
        "size_unit": args.unit,
        "width": args.width,
        "height": args.height,
        "position_mode": args.position_mode,
        "anchor_h": args.anchor_h,
        "anchor_v": args.anchor_v,
        "edge_margin": args.edge_margin,
        "custom_position_unit": args.custom_unit,
        "custom_x": args.custom_x,
        "custom_y": args.custom_y,
        "monitor_policy": args.monitor_policy,
        "relocate_on_toggle": args.relocate_on_toggle,
        "direction": args.direction,
        "animation_type": args.animation,
        "animation_scale": args.scale,
        "opacity": args.opacity,
        "corner_radius": args.corner_radius,
        "hide_on_click_outside": args.hide_on_click_outside,
        "keep_above": args.keep_above,
        "show_in_taskbar": args.show_in_taskbar,
    }
    for key, val in direct_updates.items():
        if val is not None:
            data[key] = val

    if args.clear_shortcut:
        data["shortcut"] = ""
        data["shortcut_enabled"] = False
    else:
        if args.shortcut is not None:
            data["shortcut"] = args.shortcut
        if args.shortcut_enabled is not None:
            data["shortcut_enabled"] = args.shortcut_enabled
        elif args.shortcut is not None:
            data["shortcut_enabled"] = bool(args.shortcut and not args.no_register)
        if args.no_register:
            data["shortcut_enabled"] = False

    updated = DropdownProfile.from_dict(data)

    if updated.shortcut and updated.shortcut_enabled:
        conflicts = find_profile_shortcut_conflicts(
            store.list_profiles(),
            updated.shortcut,
            exclude_profile_id=updated.id,
        )
        if conflicts:
            names = ", ".join(f"{c.name} ({c.id})" for c in conflicts)
            print(
                _("Warning       : shortcut '{shortcut}' is already assigned to: {names}.").format(
                    shortcut=updated.shortcut,
                    names=names,
                ),
                file=sys.stderr,
            )

    gen = ScriptGenerator(store.paths)
    script_path = gen.generate(updated)

    shortcut_msg = _("(no shortcut)")
    try:
        _wb, shortcut_backend = get_backends(store.paths)
    except UnsupportedBackendError as exc:
        shortcut_backend = None
        backend_err = exc
    else:
        backend_err = None

    wants_removal = bool(
        args.clear_shortcut
        or args.shortcut_enabled is False
        or not updated.shortcut
        or not updated.shortcut_enabled
    )

    if wants_removal:
        if shortcut_backend is not None and (
            args.clear_shortcut
            or args.shortcut_enabled is False
            or old_enabled
            or old_shortcut
        ):
            try:
                removed = shortcut_backend.remove_shortcut(updated)
                shortcut_msg = (
                    _("removed from desktop")
                    if removed
                    else _("(disabled / unassigned)")
                )
            except BackendError as exc:
                shortcut_msg = _("remove warning: {error}").format(error=exc)
        else:
            shortcut_msg = (
                _("(unassigned)")
                if not updated.shortcut
                else _("{shortcut} (disabled)").format(shortcut=updated.shortcut)
            )
    elif updated.shortcut and updated.shortcut_enabled:
        if args.no_register:
            shortcut_msg = _("{shortcut} (saved, not registered)").format(
                shortcut=updated.shortcut
            )
        elif shortcut_backend is None:
            updated.shortcut_enabled = False
            shortcut_msg = _("{shortcut} (saved, but not registered: {error})").format(
                shortcut=updated.shortcut,
                error=backend_err,
            )
        else:
            try:
                shortcut_backend.register_shortcut(updated)
                shortcut_msg = _("{shortcut} (registered in {backend})").format(
                    shortcut=updated.shortcut,
                    backend=shortcut_backend.name,
                )
            except BackendError as exc:
                updated.shortcut_enabled = False
                shortcut_msg = _("{shortcut} (saved, but not registered: {error})").format(
                    shortcut=updated.shortcut,
                    error=exc,
                )

    profile_path = store.save_profile(updated)
    print(_("Profile updated : {id} -> {path}").format(id=updated.id, path=profile_path))
    print(_("Script updated  : {path}").format(path=script_path))
    print(_("Shortcut        : {status}").format(status=shortcut_msg))
    return 0


def _cmd_profile_export(args: argparse.Namespace) -> int:
    from spawnkey.core.manager import SpawnKeyManager

    manager = SpawnKeyManager(AppPaths.from_env())
    ids = args.ids if args.ids else None
    if args.output:
        out_path = manager.export_profiles_to_file(Path(args.output), profile_ids=ids)
        print(_("Exported profiles to: {path}").format(path=out_path))
        return 0

    payload = manager.export_profiles(profile_ids=ids)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_profile_import(args: argparse.Namespace) -> int:
    from spawnkey.core.manager import SpawnKeyManager

    manager = SpawnKeyManager(AppPaths.from_env())
    if args.file == "-":
        raw_text = sys.stdin.read()
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(_("Invalid JSON from stdin: {error}").format(error=exc)) from exc
        result = manager.import_profiles(
            payload,
            overwrite=args.overwrite,
            register_shortcuts=not args.no_register,
        )
    else:
        result = manager.import_profiles_from_file(
            Path(args.file),
            overwrite=args.overwrite,
            register_shortcuts=not args.no_register,
        )

    for warning in result.warnings:
        print(_("Warning: {msg}").format(msg=warning), file=sys.stderr)

    print(
        _("Imported: {imported}, Updated: {updated}, Skipped: {skipped}").format(
            imported=len(result.imported),
            updated=len(result.updated),
            skipped=len(result.skipped),
        )
    )
    if result.imported:
        print(_("  Imported IDs: {ids}").format(ids=", ".join(result.imported)))
    if result.updated:
        print(_("  Updated IDs : {ids}").format(ids=", ".join(result.updated)))
    if result.skipped:
        print(_("  Skipped IDs : {ids}").format(ids=", ".join(result.skipped)))
    return 0


def _cmd_profile_preview(args: argparse.Namespace) -> int:
    from spawnkey.core.geometry import (
        compute_animation_duration_ms,
        compute_geometry,
        compute_slide_start,
    )
    from spawnkey.core.models import MonitorRect

    store = ProfileStore()
    profile = store.get_profile(args.id)
    if not profile:
        print(
            _("Error: profile '{id}' not found.").format(id=args.id),
            file=sys.stderr,
        )
        return 1

    if args.monitor:
        monitor = _parse_monitor(args.monitor)
    else:
        try:
            window_backend, _sb = get_backends(store.paths)
            monitor = window_backend.get_active_monitor(profile)
        except UnsupportedBackendError:
            monitor = MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    geom = compute_geometry(profile, monitor)
    start_x, start_y = compute_slide_start(geom, monitor, profile.direction)
    duration = compute_animation_duration_ms(profile)
    yes = _("yes")
    no = _("no")

    print(_("Profile   : {name} ({id})").format(name=profile.name, id=profile.id))
    print(
        _("Monitor   : {w}x{h} @ ({x}, {y}) [policy: {policy}]").format(
            w=monitor.width,
            h=monitor.height,
            x=monitor.x,
            y=monitor.y,
            policy=profile.monitor_policy.value,
        )
    )
    print(
        _("Target    : {w}x{h} @ ({x}, {y}) [edge margin: {margin}px]").format(
            w=geom.width,
            h=geom.height,
            x=geom.x,
            y=geom.y,
            margin=profile.edge_margin,
        )
    )
    print(
        _(
            "Appearance: opacity={opacity}% | corner_radius={radius}px | keep_above={above}"
        ).format(
            opacity=round(profile.opacity * 100),
            radius=profile.corner_radius,
            above=yes if profile.keep_above else no,
        )
    )
    print(
        _(
            "Animation : {anim} | Duration: {duration}ms | Slide start: ({sx}, {sy})"
        ).format(
            anim=profile.animation_type.value,
            duration=duration,
            sx=start_x,
            sy=start_y,
        )
    )
    print(
        _("Scale slider enabled: {value}").format(
            value=yes if profile.animation_scale_enabled else no
        )
    )
    return 0


def _cmd_profile_delete(args: argparse.Namespace) -> int:
    from spawnkey.core.script_gen import ScriptGenerator

    store = ProfileStore()
    gen = ScriptGenerator(store.paths)
    state_store = RuntimeStateStore(store.paths)

    profile = store.get_profile(args.id)
    removed_shortcut = False

    if profile is not None:
        try:
            window_backend, shortcut_backend = get_backends(store.paths)
            removed_shortcut = shortcut_backend.remove_shortcut(profile)
            state = state_store.load_state(profile.id)
            window_backend.hide_or_close(profile, state)
        except (UnsupportedBackendError, BackendError):
            pass

    deleted_profile = store.delete_profile(args.id)
    deleted_script = gen.remove(args.id)
    state_store.clear_state(args.id)

    if not deleted_profile and not deleted_script and not removed_shortcut:
        print(
            _("No profile, script, or shortcut existed with id '{id}'.").format(
                id=args.id
            ),
            file=sys.stderr,
        )
        return 1

    yes = _("yes")
    no = _("no")
    print(
        _("Deleted '{id}': profile={profile}, script={script}, shortcut={shortcut}").format(
            id=args.id,
            profile=yes if deleted_profile else no,
            script=yes if deleted_script else no,
            shortcut=yes if removed_shortcut else no,
        )
    )
    return 0


def _cmd_shortcut_list(_args: argparse.Namespace) -> int:
    from spawnkey.backends.base import ShortcutRegistrationStatus
    from spawnkey.core.shortcuts import normalize_shortcut_key

    store = ProfileStore()
    _wb, shortcut_backend = get_backends(store.paths)
    profiles = store.list_profiles()
    managed_ids = set(shortcut_backend.list_managed_shortcut_ids())
    known_components: set[str] = set()
    by_key: dict[str, list[str]] = {}

    print(_("Shortcut manager ({backend}):").format(backend=shortcut_backend.name))
    if not profiles:
        print("  " + _("(No SpawnKey profiles configured)"))
    else:
        labels = {
            ShortcutRegistrationStatus.ACTIVE: _("Active"),
            ShortcutRegistrationStatus.UNASSIGNED: _("Unassigned"),
            ShortcutRegistrationStatus.DISABLED: _("Disabled"),
            ShortcutRegistrationStatus.OUT_OF_SYNC: _("Out of sync"),
        }
        for p in profiles:
            known_components.add(p.shortcut_backend_id)
            status = shortcut_backend.get_shortcut_status(p)
            print(
                f"  - {p.id} ({p.name}): {p.shortcut or _('(none)')} -> {labels[status]}"
            )
            if p.shortcut and p.shortcut_enabled:
                norm = normalize_shortcut_key(p.shortcut)
                if norm:
                    by_key.setdefault(norm, []).append(f"{p.name} ({p.id})")

    conflicts = {k: v for k, v in by_key.items() if len(v) > 1}
    if conflicts:
        print("\n" + _("Shortcut conflicts detected between profiles:"))
        for key, owners in sorted(conflicts.items()):
            print(f"  - {key}: {', '.join(owners)}")

    orphans = sorted(managed_ids - known_components)
    if orphans:
        print(
            "\n"
            + _(
                "Orphaned shortcuts detected on the system (run `spawnkey shortcut cleanup`):"
            )
        )
        for comp in orphans:
            print(f"  - {comp}")
    return 0


def _cmd_shortcut_apply(args: argparse.Namespace) -> int:
    from spawnkey.core.script_gen import ScriptGenerator
    from spawnkey.core.shortcuts import find_profile_shortcut_conflicts

    store = ProfileStore()
    gen = ScriptGenerator(store.paths)
    profile = store.get_profile(args.id)
    if not profile:
        print(
            _("Error: profile '{id}' not found.").format(id=args.id),
            file=sys.stderr,
        )
        return 1

    if args.keys:
        profile.shortcut = args.keys
    if not profile.shortcut:
        print(
            _("Error: specify a key combination with --keys (e.g. --keys 'Meta+Shift+T')."),
            file=sys.stderr,
        )
        return 1

    conflicts = find_profile_shortcut_conflicts(
        store.list_profiles(),
        profile.shortcut,
        exclude_profile_id=profile.id,
    )
    if conflicts:
        names = ", ".join(f"{c.name} ({c.id})" for c in conflicts)
        print(
            _("Warning: shortcut '{shortcut}' is already assigned to: {names}.").format(
                shortcut=profile.shortcut,
                names=names,
            ),
            file=sys.stderr,
        )

    gen.generate(profile)
    _wb, shortcut_backend = get_backends(store.paths)
    shortcut_backend.register_shortcut(profile)
    store.save_profile(profile)
    print(
        _("Shortcut registered: {id} -> {shortcut} ({backend_id})").format(
            id=profile.id,
            shortcut=profile.shortcut,
            backend_id=profile.shortcut_backend_id,
        )
    )
    return 0


def _cmd_shortcut_remove(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = store.get_profile(args.id)
    if not profile:
        print(
            _("Error: profile '{id}' not found.").format(id=args.id),
            file=sys.stderr,
        )
        return 1

    _wb, shortcut_backend = get_backends(store.paths)
    removed = shortcut_backend.remove_shortcut(profile)
    profile.shortcut_enabled = False
    if not args.keep_keys:
        profile.shortcut = ""
    store.save_profile(profile)

    print(
        _("Shortcut removed for '{id}' (cleaned from desktop: {cleaned}).").format(
            id=profile.id,
            cleaned=_("yes") if removed else _("was not registered"),
        )
    )
    return 0


def _cmd_shortcut_cleanup(_args: argparse.Namespace) -> int:
    store = ProfileStore()
    _wb, shortcut_backend = get_backends(store.paths)
    known = {p.shortcut_backend_id for p in store.list_profiles()}
    managed = shortcut_backend.list_managed_shortcut_ids()
    orphans = [comp for comp in managed if comp not in known]

    if not orphans:
        print(_("No orphaned SpawnKey shortcuts found on the system."))
        return 0

    for comp in orphans:
        shortcut_backend.remove_shortcut_by_component(comp)
        print(_("Cleaned orphaned shortcut: {id}").format(id=comp))
    print(
        ngettext(
            "Cleaned {count} orphaned shortcut.",
            "Cleaned {count} orphaned shortcuts.",
            len(orphans),
        ).format(count=len(orphans))
    )
    return 0


def _run_profile_id(
    profile_id: str,
    *,
    hide: bool = False,
    dry_run: bool = False,
    paths: AppPaths | None = None,
    debounce_ms: int = DEFAULT_RUN_DEBOUNCE_MS,
) -> int:
    """Minimal-import hot path for `spawnkey run <id>` with non-blocking lock and debounce."""
    store = ProfileStore(paths)
    if not store.profile_path(profile_id).exists():
        print(
            _("Error: profile '{id}' not found.").format(id=profile_id),
            file=sys.stderr,
        )
        return 1

    if dry_run:
        profile = store.get_profile(profile_id)
        if not profile:
            print(
                _("Error: profile '{id}' not found.").format(id=profile_id),
                file=sys.stderr,
            )
            return 1
        from spawnkey.backends.detector import detect_environment

        info = detect_environment()
        print(
            f"[dry-run] {profile.id} ({profile.name}) -> backend={info.backend.value}, mode={profile.mode.value}"
        )
        return 0

    if hide:
        profile = store.get_profile(profile_id)
        if not profile:
            print(
                _("Error: profile '{id}' not found.").format(id=profile_id),
                file=sys.stderr,
            )
            return 1
        state_store = RuntimeStateStore(store.paths)
        window_backend, _sb = get_backends(store.paths)
        state = state_store.load_state(profile.id)
        new_state = window_backend.hide_or_close(profile, state)
        if new_state.pid is None and not new_state.visible and not new_state.window_id:
            state_store.clear_state(profile.id)
        else:
            state_store.save_state(new_state)
        return 0

    with profile_run_guard(
        store.paths,
        profile_id,
        debounce_ms=debounce_ms,
        non_blocking=True,
    ) as acquired:
        if not acquired:
            return 0

        profile = store.get_profile(profile_id)
        if not profile:
            print(
                _("Error: profile '{id}' not found.").format(id=profile_id),
                file=sys.stderr,
            )
            return 1

        state_store = RuntimeStateStore(store.paths)
        window_backend, _sb = get_backends(store.paths)
        state = state_store.load_state(profile.id)
        new_state = window_backend.toggle(profile, state)

        if new_state.pid is None and not new_state.visible and not new_state.window_id:
            state_store.clear_state(profile.id)
        else:
            state_store.save_state(new_state)
        return 0


def _cmd_run(args: argparse.Namespace) -> int:
    return _run_profile_id(args.id, hide=args.hide, dry_run=args.dry_run)


def _cmd_gui(_args: argparse.Namespace) -> int:
    from spawnkey.gui.app import launch_gui

    return launch_gui([])


def build_parser() -> argparse.ArgumentParser:
    import argparse

    from spawnkey.core.models import (
        AnchorH,
        AnchorV,
        AnimationType,
        LifecycleMode,
        MonitorPolicy,
        PositionMode,
        SizeUnit,
        SlideDirection,
    )

    parser = argparse.ArgumentParser(prog="spawnkey", description=_("SpawnKey CLI and GUI"))
    sub = parser.add_subparsers(dest="command", required=False)

    p_gui = sub.add_parser("gui", help=_("Open the SpawnKey GTK 4 graphical interface"))
    p_gui.set_defaults(func=_cmd_gui)

    p_info = sub.add_parser("info", help=_("Show detected environment and XDG paths"))
    p_info.set_defaults(func=_cmd_info)

    p_doc = sub.add_parser(
        "doctor", help=_("Diagnose environment, backends, scripts, and shortcuts")
    )
    p_doc.set_defaults(func=_cmd_doctor)

    p_apps = sub.add_parser(
        "apps", help=_("List .desktop applications discovered on the system")
    )
    p_apps.add_argument(
        "--query", default="", help=_("Filter by name, ID, or comment")
    )
    p_apps.add_argument(
        "--limit", type=int, default=20, help=_("Result limit (0 = all)")
    )
    p_apps.set_defaults(func=_cmd_apps)

    # Profile commands
    p_prof = sub.add_parser("profile", help=_("Manage profiles and generated scripts"))
    prof_sub = p_prof.add_subparsers(dest="profile_cmd", required=True)

    p_list = prof_sub.add_parser("list", help=_("List saved profiles"))
    p_list.set_defaults(func=_cmd_profile_list)

    p_show = prof_sub.add_parser("show", help=_("Show full configuration for a profile"))
    p_show.add_argument("id", help=_("Profile ID"))
    p_show.add_argument(
        "--json", action="store_true", help=_("Print profile as JSON")
    )
    p_show.set_defaults(func=_cmd_profile_show)

    p_create = prof_sub.add_parser(
        "create", help=_("Create a profile, its .sh script, and register its shortcut")
    )
    src_group = p_create.add_mutually_exclusive_group(required=True)
    src_group.add_argument(
        "--desktop", help=_(".desktop file ID (e.g. Alacritty.desktop)")
    )
    src_group.add_argument(
        "--exec", dest="exec_cmd", help=_("Command or path to executable file")
    )
    p_create.add_argument("--id", help=_("Explicit profile ID (optional)"))
    p_create.add_argument("--name", help=_("Visible dropdown name"))
    p_create.add_argument("--match-hint", help=_("Window class (WM_CLASS or app_id)"))
    p_create.add_argument(
        "--extra-args", default="", help=_("Optional launch arguments")
    )
    p_create.add_argument(
        "--mode", choices=[m.value for m in LifecycleMode], default="persistent"
    )
    p_create.add_argument(
        "--startup-timeout-ms",
        type=int,
        default=8000,
        help=_("Maximum wait when capturing a new window in ms (1000 to 60000)"),
    )
    p_create.add_argument(
        "--unit", choices=[u.value for u in SizeUnit], default="percent"
    )
    p_create.add_argument("--width", type=float, default=80.0)
    p_create.add_argument("--height", type=float, default=45.0)
    p_create.add_argument(
        "--position-mode", choices=[p.value for p in PositionMode], default="preset"
    )
    p_create.add_argument(
        "--anchor-h", choices=[a.value for a in AnchorH], default="center"
    )
    p_create.add_argument(
        "--anchor-v", choices=[a.value for a in AnchorV], default="top"
    )
    p_create.add_argument(
        "--edge-margin",
        type=int,
        default=0,
        help=_("Edge margin in px (3x3 grid)"),
    )
    p_create.add_argument(
        "--custom-unit",
        choices=[u.value for u in SizeUnit],
        default=None,
        help=_("Custom position unit (defaults to --unit)"),
    )
    p_create.add_argument("--custom-x", type=float, default=10.0)
    p_create.add_argument("--custom-y", type=float, default=0.0)
    p_create.add_argument(
        "--monitor-policy", choices=[m.value for m in MonitorPolicy], default="cursor"
    )
    p_create.add_argument(
        "--no-relocate-on-toggle",
        action="store_true",
        help=_("Do not relocate existing window to the active monitor on toggle"),
    )
    p_create.add_argument(
        "--direction",
        choices=[d.value for d in SlideDirection],
        default="top",
        help=_("Edge from which the window slides in"),
    )
    p_create.add_argument(
        "--animation",
        choices=[a.value for a in AnimationType],
        default="slide-fade",
        help=_("Animation effect"),
    )
    p_create.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help=_("Animation duration factor (0.1 to 5.0)"),
    )
    p_create.add_argument(
        "--opacity", type=float, default=1.0, help=_("Opacity between 0.1 and 1.0")
    )
    p_create.add_argument(
        "--corner-radius", type=int, default=0, help=_("Rounded corners in px")
    )
    p_create.add_argument("--no-click-outside", action="store_true")
    p_create.add_argument(
        "--no-keep-above",
        action="store_true",
        help=_("Do not force the window always on top"),
    )
    p_create.add_argument("--show-in-taskbar", action="store_true")
    p_create.add_argument("--shortcut", default="")
    p_create.add_argument(
        "--no-register",
        action="store_true",
        help=_("Do not register the shortcut in the desktop yet"),
    )
    p_create.set_defaults(func=_cmd_profile_create)

    p_edit = prof_sub.add_parser("edit", help=_("Edit an existing profile"))
    p_edit.add_argument("id", help=_("Profile ID"))
    p_edit.add_argument("--name", default=None, help=_("Visible dropdown name"))
    edit_src = p_edit.add_mutually_exclusive_group(required=False)
    edit_src.add_argument(
        "--desktop", default=None, help=_(".desktop file ID (e.g. Alacritty.desktop)")
    )
    edit_src.add_argument(
        "--exec", dest="exec_cmd", default=None, help=_("Command or path to executable file")
    )
    p_edit.add_argument(
        "--match-hint", default=None, help=_("Window class (WM_CLASS or app_id)")
    )
    p_edit.add_argument(
        "--extra-args", default=None, help=_("Optional launch arguments")
    )
    p_edit.add_argument(
        "--mode", choices=[m.value for m in LifecycleMode], default=None
    )
    p_edit.add_argument(
        "--startup-timeout-ms",
        type=int,
        default=None,
        help=_("Maximum wait when capturing a new window in ms (1000 to 60000)"),
    )
    p_edit.add_argument("--unit", choices=[u.value for u in SizeUnit], default=None)
    p_edit.add_argument("--width", type=float, default=None)
    p_edit.add_argument("--height", type=float, default=None)
    p_edit.add_argument(
        "--position-mode", choices=[p.value for p in PositionMode], default=None
    )
    p_edit.add_argument("--anchor-h", choices=[a.value for a in AnchorH], default=None)
    p_edit.add_argument("--anchor-v", choices=[a.value for a in AnchorV], default=None)
    p_edit.add_argument(
        "--edge-margin",
        type=int,
        default=None,
        help=_("Edge margin in px (3x3 grid)"),
    )
    p_edit.add_argument(
        "--custom-unit",
        choices=[u.value for u in SizeUnit],
        default=None,
        help=_("Custom position unit"),
    )
    p_edit.add_argument("--custom-x", type=float, default=None)
    p_edit.add_argument("--custom-y", type=float, default=None)
    p_edit.add_argument(
        "--monitor-policy", choices=[m.value for m in MonitorPolicy], default=None
    )
    relocate_grp = p_edit.add_mutually_exclusive_group(required=False)
    relocate_grp.add_argument(
        "--relocate-on-toggle",
        dest="relocate_on_toggle",
        action="store_true",
        default=None,
        help=_("Relocate existing window to active monitor on toggle"),
    )
    relocate_grp.add_argument(
        "--no-relocate-on-toggle",
        dest="relocate_on_toggle",
        action="store_false",
        help=_("Keep existing window on its current monitor on toggle"),
    )
    p_edit.add_argument(
        "--direction",
        choices=[d.value for d in SlideDirection],
        default=None,
        help=_("Edge from which the window slides in"),
    )
    p_edit.add_argument(
        "--animation",
        choices=[a.value for a in AnimationType],
        default=None,
        help=_("Animation effect"),
    )
    p_edit.add_argument(
        "--scale",
        type=float,
        default=None,
        help=_("Animation duration factor (0.1 to 5.0)"),
    )
    p_edit.add_argument(
        "--opacity", type=float, default=None, help=_("Opacity between 0.1 and 1.0")
    )
    p_edit.add_argument(
        "--corner-radius", type=int, default=None, help=_("Rounded corners in px")
    )
    click_grp = p_edit.add_mutually_exclusive_group(required=False)
    click_grp.add_argument(
        "--click-outside",
        dest="hide_on_click_outside",
        action="store_true",
        default=None,
        help=_("Hide when clicking outside the window"),
    )
    click_grp.add_argument(
        "--no-click-outside",
        dest="hide_on_click_outside",
        action="store_false",
        help=_("Do not hide when clicking outside the window"),
    )
    above_grp = p_edit.add_mutually_exclusive_group(required=False)
    above_grp.add_argument(
        "--keep-above",
        dest="keep_above",
        action="store_true",
        default=None,
        help=_("Keep the window always on top"),
    )
    above_grp.add_argument(
        "--no-keep-above",
        dest="keep_above",
        action="store_false",
        help=_("Do not force the window always on top"),
    )
    taskbar_grp = p_edit.add_mutually_exclusive_group(required=False)
    taskbar_grp.add_argument(
        "--show-in-taskbar",
        dest="show_in_taskbar",
        action="store_true",
        default=None,
        help=_("Show window in the taskbar"),
    )
    taskbar_grp.add_argument(
        "--no-show-in-taskbar",
        dest="show_in_taskbar",
        action="store_false",
        help=_("Hide window from the taskbar"),
    )
    p_edit.add_argument("--shortcut", default=None)
    p_edit.add_argument(
        "--clear-shortcut",
        action="store_true",
        help=_("Remove shortcut key binding from profile and desktop"),
    )
    sc_enable_grp = p_edit.add_mutually_exclusive_group(required=False)
    sc_enable_grp.add_argument(
        "--enable-shortcut",
        dest="shortcut_enabled",
        action="store_true",
        default=None,
        help=_("Enable shortcut registration"),
    )
    sc_enable_grp.add_argument(
        "--disable-shortcut",
        dest="shortcut_enabled",
        action="store_false",
        help=_("Disable shortcut without clearing keys"),
    )
    p_edit.add_argument(
        "--no-register",
        action="store_true",
        help=_("Do not register the shortcut in the desktop yet"),
    )
    p_edit.set_defaults(func=_cmd_profile_edit)

    p_export = prof_sub.add_parser(
        "export", help=_("Export profiles to a portable JSON bundle")
    )
    p_export.add_argument(
        "ids", nargs="*", help=_("Profile IDs to export (empty = all)")
    )
    p_export.add_argument(
        "-o", "--output", default="", help=_("Output JSON file (omit for stdout)")
    )
    p_export.set_defaults(func=_cmd_profile_export)

    p_import = prof_sub.add_parser(
        "import", help=_("Import profiles from a JSON file or '-' for stdin")
    )
    p_import.add_argument(
        "file", help=_("Input JSON file path, or '-' to read from stdin")
    )
    p_import.add_argument(
        "--overwrite",
        action="store_true",
        help=_("Overwrite existing profiles with the same ID"),
    )
    p_import.add_argument(
        "--no-register",
        action="store_true",
        help=_("Do not register shortcuts on the desktop"),
    )
    p_import.set_defaults(func=_cmd_profile_import)

    p_prev = prof_sub.add_parser(
        "preview", help=_("Compute exact geometry for a given monitor")
    )
    p_prev.add_argument("id", help=_("Profile ID"))
    p_prev.add_argument(
        "--monitor",
        default="",
        help=_("Monitor resolution (e.g. 1920x1080); empty = active monitor"),
    )
    p_prev.set_defaults(func=_cmd_profile_preview)

    p_del = prof_sub.add_parser(
        "delete", help=_("Delete a profile, its .sh script, and its system shortcut")
    )
    p_del.add_argument("id", help=_("Profile ID"))
    p_del.set_defaults(func=_cmd_profile_delete)

    # Shortcut manager commands
    p_sc = sub.add_parser(
        "shortcut", help=_("Manage global shortcuts created by SpawnKey")
    )
    sc_sub = p_sc.add_subparsers(dest="shortcut_cmd", required=True)

    p_sc_list = sc_sub.add_parser(
        "list", help=_("List all created shortcuts and their actual status")
    )
    p_sc_list.set_defaults(func=_cmd_shortcut_list)

    p_sc_apply = sc_sub.add_parser(
        "apply", help=_("Register or update a profile shortcut on the desktop")
    )
    p_sc_apply.add_argument("id", help=_("Profile ID"))
    p_sc_apply.add_argument(
        "--keys",
        default="",
        help=_("Key combination (e.g. 'Meta+Shift+T')"),
    )
    p_sc_apply.set_defaults(func=_cmd_shortcut_apply)

    p_sc_rm = sc_sub.add_parser(
        "remove", help=_("Remove the desktop shortcut without deleting the profile")
    )
    p_sc_rm.add_argument("id", help=_("Profile ID"))
    p_sc_rm.add_argument(
        "--keep-keys",
        action="store_true",
        help=_("Disable the shortcut while keeping the saved key combination"),
    )
    p_sc_rm.set_defaults(func=_cmd_shortcut_remove)

    p_sc_clean = sc_sub.add_parser(
        "cleanup", help=_("Remove orphaned SpawnKey shortcuts from the desktop")
    )
    p_sc_clean.set_defaults(func=_cmd_shortcut_cleanup)

    # Runner command
    p_run = sub.add_parser(
        "run", help=_("Toggle (show/hide/close) a profile's dropdown")
    )
    p_run.add_argument("id", help=_("Profile ID"))
    p_run.add_argument(
        "--hide",
        action="store_true",
        help=_("Force hide (persistent) or close (ephemeral)"),
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help=_("Verify profile loading without opening windows"),
    )
    p_run.set_defaults(func=_cmd_run)

    return parser


def _try_fast_run(raw_argv: list[str]) -> int | None:
    """Bypass argparse for `spawnkey run <id> [--hide] [--dry-run]` to minimize cold start."""
    if not raw_argv or raw_argv[0] != "run":
        return None
    profile_id: str | None = None
    hide = False
    dry_run = False
    for tok in raw_argv[1:]:
        if tok == "--hide":
            hide = True
        elif tok == "--dry-run":
            dry_run = True
        elif tok.startswith("-"):
            return None
        elif profile_id is None:
            profile_id = tok
        else:
            return None
    if not profile_id:
        return None
    return _run_profile_id(profile_id, hide=hide, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        fast_rc = _try_fast_run(raw_argv)
        if fast_rc is not None:
            return fast_rc
        parser = build_parser()
        args = parser.parse_args(raw_argv)
        if not getattr(args, "command", None):
            return _cmd_gui(args)
        return int(args.func(args))
    except (ValueError, UnsupportedBackendError, BackendError) as exc:
        print(_("Error: {error}").format(error=exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
