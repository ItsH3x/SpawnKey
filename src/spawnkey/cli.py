"""Command-line interface for SpawnKey profiles, runner, and shortcut manager."""

from __future__ import annotations

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


def _parse_monitor(spec: str):
    from spawnkey.core.models import MonitorRect

    parts = spec.lower().split("x", 1)
    if len(parts) != 2:
        raise ValueError(f"Formato de monitor inválido '{spec}', usa ANCHOxALTO (ej. 1920x1080).")
    return MonitorRect(x=0, y=0, width=int(parts[0]), height=int(parts[1]), name="preview")


def _cmd_info(_args: argparse.Namespace) -> int:
    from spawnkey.backends.detector import detect_environment

    info = detect_environment()
    paths = AppPaths.from_env()
    available = [name for name, ok in sorted(info.tools.items()) if ok]

    print(f"Sesión gráfica : {info.session_type}")
    print(f"Escritorio     : {info.desktop}")
    print(f"Backend        : {info.backend.value}")
    try:
        wb, _ = get_backends(paths, env_info=info)
        caps = wb.capabilities
        print(
            f"Capacidades    : opacidad={'sí' if caps.supports_opacity else 'no'} | "
            f"redondeo={'sí' if caps.supports_corner_radius else 'no'} | "
            f"clic_fuera={'sí' if caps.supports_click_outside else 'no'} | "
            f"animaciones={'sí' if caps.supports_animations else 'no'}"
        )
    except UnsupportedBackendError:
        pass
    print(f"Herramientas   : {', '.join(available) if available else '(ninguna detectada)'}")
    print(f"Perfiles (XDG) : {paths.profiles_dir}")
    print(f"Scripts  (XDG) : {paths.scripts_dir}")
    print(f"Estado   (XDG) : {paths.state_dir}")
    return 0


def _cmd_apps(args: argparse.Namespace) -> int:
    from spawnkey.core.desktop_apps import discover_desktop_apps

    apps = discover_desktop_apps(query=args.query)
    total = len(apps)
    shown = apps[: args.limit] if args.limit > 0 else apps
    print(
        f"Aplicaciones encontradas: {total}"
        + (f" (mostrando {len(shown)})" if len(shown) < total else "")
    )
    for app in shown:
        term_tag = " [Terminal]" if app.terminal else ""
        print(f"  - {app.name}{term_tag} [{app.desktop_id}] -> {app.exec_cmd}")
    return 0


def _cmd_profile_list(_args: argparse.Namespace) -> int:
    from spawnkey.core.models import PositionMode

    store = ProfileStore()
    profiles = store.list_profiles()
    if not profiles:
        print("No hay perfiles configurados todavía.")
        return 0

    shortcut_backend = None
    try:
        _, shortcut_backend = get_backends(store.paths)
    except UnsupportedBackendError:
        pass

    for p in profiles:
        if p.position_mode is PositionMode.PRESET:
            margin_str = f" (margen {p.edge_margin}px)" if p.edge_margin > 0 else ""
            pos = f"anclaje {p.anchor_v.value}-{p.anchor_h.value}{margin_str}"
        else:
            pos = f"personalizada ({p.custom_x}, {p.custom_y} {p.custom_position_unit.value})"
        anim = (
            "ninguna (escala deshabilitada)"
            if not p.animation_scale_enabled
            else f"{p.animation_type.value} desde {p.direction.value} (x{p.animation_scale})"
        )
        status_str = ""
        if shortcut_backend is not None:
            status = shortcut_backend.get_shortcut_status(p)
            status_str = f" [{status.value}]"
        extra_str = f" {p.extra_args}" if p.extra_args else ""
        print(
            f"- {p.id}: {p.name}\n"
            f"    Cmd      : {p.exec_cmd}{extra_str}\n"
            f"    Modo     : {p.mode.label} — {p.mode.short_description}\n"
            f"    Tamaño   : {p.width}x{p.height} ({p.size_unit.value}) | Posición: {pos} | Monitor: {p.monitor_policy.value}\n"
            f"    Animación: {anim}\n"
            f"    Opciones : borderless=sí | encima={'sí' if p.keep_above else 'no'} | "
            f"clic_fuera={'sí' if p.hide_on_click_outside else 'no'} | "
            f"barra_tareas={'sí' if p.show_in_taskbar else 'no'} | "
            f"opacidad={round(p.opacity * 100)}% | redondeo={p.corner_radius}px | "
            f"espera={p.startup_timeout_ms}ms\n"
            f"    Atajo    : {p.shortcut or '(sin asignar)'}{status_str} | Script: {p.script_path or '(no generado)'}"
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
            (a for a in apps if a.desktop_id == args.desktop or a.desktop_id == f"{args.desktop}.desktop"),
            None,
        )
        if not match:
            print(f"Error: no se encontró la app .desktop '{args.desktop}'.", file=sys.stderr)
            return 1
        source_type = SourceType.DESKTOP
        desktop_id = match.desktop_id
        exec_cmd = wrap_terminal_command(match.exec_cmd) if match.terminal else match.exec_cmd
        name = name or match.name
        match_hint = match_hint or match.wm_class
    else:
        ok, detail = validate_executable_command(exec_cmd)
        if not ok:
            print(f"Error en ejecutable: {detail}", file=sys.stderr)
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
                f"Aviso         : el atajo '{profile.shortcut}' ya está asignado a: {names}.",
                file=sys.stderr,
            )

    script_path = gen.generate(profile)
    registered_msg = "(sin atajo)"
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
            registered_msg = f"{profile.shortcut} (guardado, pero no registrado: {backend_err})"
        else:
            try:
                shortcut_backend.register_shortcut(profile)
                registered_msg = f"{profile.shortcut} (registrado en {shortcut_backend.name})"
            except BackendError as exc:
                profile.shortcut_enabled = False
                registered_msg = f"{profile.shortcut} (guardado, pero no registrado: {exc})"

    profile_path = store.save_profile(profile)
    print(f"Perfil creado : {profile.id} -> {profile_path}")
    print(f"Script creado : {script_path}")
    print(f"Atajo         : {registered_msg}")
    if (
        profile.corner_radius > 0
        and active_window_backend is not None
        and not active_window_backend.capabilities.supports_corner_radius
    ):
        print(
            f"Aviso         : el backend activo ({active_window_backend.name}) no soporta redondeo de esquinas; "
            f"el valor ({profile.corner_radius}px) se guardó para compositores compatibles.",
            file=sys.stderr,
        )
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
        print(f"Error: perfil '{args.id}' no encontrado.", file=sys.stderr)
        return 1

    if args.monitor:
        monitor = _parse_monitor(args.monitor)
    else:
        try:
            window_backend, _ = get_backends(store.paths)
            monitor = window_backend.get_active_monitor(profile)
        except UnsupportedBackendError:
            monitor = MonitorRect(x=0, y=0, width=1920, height=1080, name="default")

    geom = compute_geometry(profile, monitor)
    start_x, start_y = compute_slide_start(geom, monitor, profile.direction)
    duration = compute_animation_duration_ms(profile)

    print(f"Perfil   : {profile.name} ({profile.id})")
    print(f"Monitor  : {monitor.width}x{monitor.height} @ ({monitor.x}, {monitor.y}) [política: {profile.monitor_policy.value}]")
    print(f"Destino  : {geom.width}x{geom.height} @ ({geom.x}, {geom.y}) [margen borde: {profile.edge_margin}px]")
    print(f"Apariencia: opacidad={round(profile.opacity * 100)}% | redondeo={profile.corner_radius}px | encima={'sí' if profile.keep_above else 'no'}")
    print(f"Animación: {profile.animation_type.value} | Duración: {duration}ms | Inicio slide: ({start_x}, {start_y})")
    print(f"Slider escala habilitado: {'sí' if profile.animation_scale_enabled else 'no'}")
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
        print(f"No existía ningún perfil, script o atajo con id '{args.id}'.", file=sys.stderr)
        return 1

    print(
        f"Eliminado '{args.id}': perfil={'sí' if deleted_profile else 'no'}, "
        f"script={'sí' if deleted_script else 'no'}, atajo={'sí' if removed_shortcut else 'no'}"
    )
    return 0


def _cmd_shortcut_list(_args: argparse.Namespace) -> int:
    from spawnkey.backends.base import ShortcutRegistrationStatus
    from spawnkey.core.shortcuts import normalize_shortcut_key

    store = ProfileStore()
    _, shortcut_backend = get_backends(store.paths)
    profiles = store.list_profiles()
    managed_ids = set(shortcut_backend.list_managed_shortcut_ids())
    known_components: set[str] = set()
    by_key: dict[str, list[str]] = {}

    print(f"Administrador de atajos ({shortcut_backend.name}):")
    if not profiles:
        print("  (No hay perfiles de SpawnKey configurados)")
    else:
        for p in profiles:
            known_components.add(p.shortcut_backend_id)
            status = shortcut_backend.get_shortcut_status(p)
            labels = {
                ShortcutRegistrationStatus.ACTIVE: "Activo",
                ShortcutRegistrationStatus.UNASSIGNED: "Sin atajo",
                ShortcutRegistrationStatus.DISABLED: "Inactivo",
                ShortcutRegistrationStatus.OUT_OF_SYNC: "Sin aplicar",
            }
            print(
                f"  - {p.id} ({p.name}): {p.shortcut or '(ninguno)'} -> {labels[status]}"
            )
            if p.shortcut and p.shortcut_enabled:
                norm = normalize_shortcut_key(p.shortcut)
                if norm:
                    by_key.setdefault(norm, []).append(f"{p.name} ({p.id})")

    conflicts = {k: v for k, v in by_key.items() if len(v) > 1}
    if conflicts:
        print("\nConflictos de atajos detectados entre perfiles:")
        for key, owners in sorted(conflicts.items()):
            print(f"  - {key}: {', '.join(owners)}")

    orphans = sorted(managed_ids - known_components)
    if orphans:
        print("\nAtajos huérfanos detectados en el sistema (usa `spawnkey shortcut cleanup`):")
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
        print(f"Error: perfil '{args.id}' no encontrado.", file=sys.stderr)
        return 1

    if args.keys:
        profile.shortcut = args.keys
    if not profile.shortcut:
        print("Error: indica una combinación con --keys (ej. --keys 'Meta+Shift+T').", file=sys.stderr)
        return 1

    conflicts = find_profile_shortcut_conflicts(
        store.list_profiles(),
        profile.shortcut,
        exclude_profile_id=profile.id,
    )
    if conflicts:
        names = ", ".join(f"{c.name} ({c.id})" for c in conflicts)
        print(
            f"Aviso: el atajo '{profile.shortcut}' ya está asignado a: {names}.",
            file=sys.stderr,
        )

    gen.generate(profile)
    _, shortcut_backend = get_backends(store.paths)
    shortcut_backend.register_shortcut(profile)
    store.save_profile(profile)
    print(f"Atajo registrado: {profile.id} -> {profile.shortcut} ({profile.shortcut_backend_id})")
    return 0


def _cmd_shortcut_remove(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = store.get_profile(args.id)
    if not profile:
        print(f"Error: perfil '{args.id}' no encontrado.", file=sys.stderr)
        return 1

    _, shortcut_backend = get_backends(store.paths)
    removed = shortcut_backend.remove_shortcut(profile)
    profile.shortcut_enabled = False
    if not args.keep_keys:
        profile.shortcut = ""
    store.save_profile(profile)

    print(
        f"Atajo removido para '{profile.id}' (limpiado del escritorio: {'sí' if removed else 'no estaba registrado'})."
    )
    return 0


def _cmd_shortcut_cleanup(_args: argparse.Namespace) -> int:
    store = ProfileStore()
    _, shortcut_backend = get_backends(store.paths)
    known = {p.shortcut_backend_id for p in store.list_profiles()}
    managed = shortcut_backend.list_managed_shortcut_ids()
    orphans = [comp for comp in managed if comp not in known]

    if not orphans:
        print("No hay atajos huérfanos de SpawnKey en el sistema.")
        return 0

    for comp in orphans:
        shortcut_backend.remove_shortcut_by_component(comp)
        print(f"Limpiado atajo huérfano: {comp}")
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
        print(f"Error: perfil '{profile_id}' no encontrado.", file=sys.stderr)
        return 1

    if dry_run:
        profile = store.get_profile(profile_id)
        if not profile:
            print(f"Error: perfil '{profile_id}' no encontrado.", file=sys.stderr)
            return 1
        from spawnkey.backends.detector import detect_environment

        info = detect_environment()
        print(f"[dry-run] {profile.id} ({profile.name}) -> backend={info.backend.value}, mode={profile.mode.value}")
        return 0

    if hide:
        profile = store.get_profile(profile_id)
        if not profile:
            print(f"Error: perfil '{profile_id}' no encontrado.", file=sys.stderr)
            return 1
        state_store = RuntimeStateStore(store.paths)
        window_backend, _ = get_backends(store.paths)
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
            print(f"Error: perfil '{profile_id}' no encontrado.", file=sys.stderr)
            return 1

        state_store = RuntimeStateStore(store.paths)
        window_backend, _ = get_backends(store.paths)
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

    parser = argparse.ArgumentParser(prog="spawnkey", description="SpawnKey CLI y GUI")
    sub = parser.add_subparsers(dest="command", required=False)

    p_gui = sub.add_parser("gui", help="Abre la interfaz gráfica GTK 4 de SpawnKey")
    p_gui.set_defaults(func=_cmd_gui)

    p_info = sub.add_parser("info", help="Muestra el entorno detectado y las rutas XDG")
    p_info.set_defaults(func=_cmd_info)

    p_apps = sub.add_parser("apps", help="Lista aplicaciones .desktop descubiertas en el sistema")
    p_apps.add_argument("--query", default="", help="Filtra por nombre, ID o comentario")
    p_apps.add_argument("--limit", type=int, default=20, help="Límite de resultados (0 = todos)")
    p_apps.set_defaults(func=_cmd_apps)

    # Profile commands
    p_prof = sub.add_parser("profile", help="Administra perfiles y scripts generados")
    prof_sub = p_prof.add_subparsers(dest="profile_cmd", required=True)

    p_list = prof_sub.add_parser("list", help="Lista los perfiles guardados")
    p_list.set_defaults(func=_cmd_profile_list)

    p_create = prof_sub.add_parser("create", help="Crea un perfil, su script .sh y registra su atajo")
    src_group = p_create.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--desktop", help="ID de archivo .desktop (ej. Alacritty.desktop)")
    src_group.add_argument("--exec", dest="exec_cmd", help="Comando o ruta a archivo ejecutable")
    p_create.add_argument("--id", help="ID explícito del perfil (opcional)")
    p_create.add_argument("--name", help="Nombre visible del dropdown")
    p_create.add_argument("--match-hint", help="Clase de ventana (WM_CLASS o app_id)")
    p_create.add_argument("--extra-args", default="", help="Argumentos opcionales al lanzar")
    p_create.add_argument("--mode", choices=[m.value for m in LifecycleMode], default="persistent")
    p_create.add_argument(
        "--startup-timeout-ms",
        type=int,
        default=8000,
        help="Espera máxima al capturar ventana nueva en ms (1000 a 60000)",
    )
    p_create.add_argument("--unit", choices=[u.value for u in SizeUnit], default="percent")
    p_create.add_argument("--width", type=float, default=80.0)
    p_create.add_argument("--height", type=float, default=45.0)
    p_create.add_argument("--position-mode", choices=[p.value for p in PositionMode], default="preset")
    p_create.add_argument("--anchor-h", choices=[a.value for a in AnchorH], default="center")
    p_create.add_argument("--anchor-v", choices=[a.value for a in AnchorV], default="top")
    p_create.add_argument("--edge-margin", type=int, default=0, help="Margen al borde en px (cuadrícula 3x3)")
    p_create.add_argument(
        "--custom-unit",
        choices=[u.value for u in SizeUnit],
        default=None,
        help="Unidad de posición personalizada (por defecto igual a --unit)",
    )
    p_create.add_argument("--custom-x", type=float, default=10.0)
    p_create.add_argument("--custom-y", type=float, default=0.0)
    p_create.add_argument("--monitor-policy", choices=[m.value for m in MonitorPolicy], default="cursor")
    p_create.add_argument("--direction", choices=[d.value for d in SlideDirection], default="top", help="Borde desde el que entra la ventana")
    p_create.add_argument("--animation", choices=[a.value for a in AnimationType], default="slide-fade", help="Efecto de animación")
    p_create.add_argument("--scale", type=float, default=1.0, help="Factor de duración de animación (0.1 a 5.0)")
    p_create.add_argument("--opacity", type=float, default=1.0, help="Opacidad entre 0.1 y 1.0")
    p_create.add_argument("--corner-radius", type=int, default=0, help="Esquinas redondeadas en px")
    p_create.add_argument("--no-click-outside", action="store_true")
    p_create.add_argument("--no-keep-above", action="store_true", help="No forzar la ventana siempre encima")
    p_create.add_argument("--show-in-taskbar", action="store_true")
    p_create.add_argument("--shortcut", default="")
    p_create.add_argument("--no-register", action="store_true", help="No registrar el atajo todavía en el escritorio")
    p_create.set_defaults(func=_cmd_profile_create)

    p_prev = prof_sub.add_parser("preview", help="Calcula geometría exacta para un monitor dado")
    p_prev.add_argument("id", help="ID del perfil")
    p_prev.add_argument("--monitor", default="", help="Resolución del monitor (ej. 1920x1080); vacío = monitor activo")
    p_prev.set_defaults(func=_cmd_profile_preview)

    p_del = prof_sub.add_parser("delete", help="Elimina un perfil, su script .sh y su atajo del sistema")
    p_del.add_argument("id", help="ID del perfil")
    p_del.set_defaults(func=_cmd_profile_delete)

    # Shortcut manager commands
    p_sc = sub.add_parser("shortcut", help="Administrador de atajos globales creados por SpawnKey")
    sc_sub = p_sc.add_subparsers(dest="shortcut_cmd", required=True)

    p_sc_list = sc_sub.add_parser("list", help="Lista todos los atajos creados y su estado real")
    p_sc_list.set_defaults(func=_cmd_shortcut_list)

    p_sc_apply = sc_sub.add_parser("apply", help="Registra o actualiza el atajo de un perfil en el escritorio")
    p_sc_apply.add_argument("id", help="ID del perfil")
    p_sc_apply.add_argument("--keys", default="", help="Combinación de teclas (ej. 'Meta+Shift+T')")
    p_sc_apply.set_defaults(func=_cmd_shortcut_apply)

    p_sc_rm = sc_sub.add_parser("remove", help="Remueve el atajo del escritorio sin borrar el perfil")
    p_sc_rm.add_argument("id", help="ID del perfil")
    p_sc_rm.add_argument("--keep-keys", action="store_true", help="Desactiva el atajo conservando la combinación guardada")
    p_sc_rm.set_defaults(func=_cmd_shortcut_remove)

    p_sc_clean = sc_sub.add_parser("cleanup", help="Elimina atajos huérfanos de SpawnKey en el escritorio")
    p_sc_clean.set_defaults(func=_cmd_shortcut_cleanup)

    # Runner command
    p_run = sub.add_parser("run", help="Alterna (abre/oculta/cierra) el dropdown de un perfil")
    p_run.add_argument("id", help="ID del perfil")
    p_run.add_argument("--hide", action="store_true", help="Fuerza ocultar (persistente) o cerrar (efímero)")
    p_run.add_argument("--dry-run", action="store_true", help="Verifica carga del perfil sin abrir ventanas")
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
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
