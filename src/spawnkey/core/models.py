"""Declarative data models and validation rules for SpawnKey profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
import re
import shlex
from typing import Any


SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SourceType(str, Enum):
    DESKTOP = "desktop"
    EXECUTABLE = "executable"


class LifecycleMode(str, Enum):
    PERSISTENT = "persistent"
    EPHEMERAL = "ephemeral"
    TRAY = "tray"

    @property
    def label(self) -> str:
        if self is LifecycleMode.PERSISTENT:
            return "Persistente (minimizar)"
        if self is LifecycleMode.EPHEMERAL:
            return "Efímero (cerrar al ocultar)"
        return "Bandeja del sistema (tray)"

    @property
    def short_description(self) -> str:
        if self is LifecycleMode.PERSISTENT:
            return "Mantiene la ventana en segundo plano; el atajo la muestra o minimiza."
        if self is LifecycleMode.EPHEMERAL:
            return "Lanza la app al mostrarla y cierra su proceso al ocultarla."
        return "Para apps con icono en bandeja; restaura su estado normal al ocultarla."


class SizeUnit(str, Enum):
    PERCENT = "percent"
    PIXELS = "pixels"


class PositionMode(str, Enum):
    PRESET = "preset"
    CUSTOM = "custom"


class AnchorH(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class AnchorV(str, Enum):
    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class SlideDirection(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


class AnimationType(str, Enum):
    SLIDE = "slide"
    FADE = "fade"
    SLIDE_FADE = "slide-fade"
    NONE = "none"


class MonitorPolicy(str, Enum):
    CURSOR = "cursor"
    FOCUSED = "focused"
    PRIMARY = "primary"


@dataclass(frozen=True)
class MonitorRect:
    """Usable screen rectangle for a monitor in global desktop coordinates."""

    x: int
    y: int
    width: int
    height: int
    name: str = ""

    def __post_init__(self) -> None:
        if self.width <= 1 or self.height <= 1:
            raise ValueError(
                f"Las dimensiones del monitor deben ser mayores a 1px: {self.width}x{self.height}"
            )


@dataclass(frozen=True)
class WindowGeometry:
    """Resolved target window geometry in global desktop coordinates."""

    x: int
    y: int
    width: int
    height: int


@dataclass
class DropdownProfile:
    """Declarative configuration for a single dropdown window and its shortcut."""

    id: str
    name: str
    exec_cmd: str
    source_type: SourceType = SourceType.EXECUTABLE
    desktop_id: str | None = None
    match_hint: str = ""
    extra_args: str = ""

    # Lifecycle
    mode: LifecycleMode = LifecycleMode.PERSISTENT
    startup_timeout_ms: int = 8000

    # Dimensions (always strictly smaller than active monitor)
    size_unit: SizeUnit = SizeUnit.PERCENT
    width: float = 80.0
    height: float = 45.0

    # Position: 3x3 preset grid or custom coordinates
    position_mode: PositionMode = PositionMode.PRESET
    anchor_h: AnchorH = AnchorH.CENTER
    anchor_v: AnchorV = AnchorV.TOP
    edge_margin: int = 0
    custom_position_unit: SizeUnit = SizeUnit.PERCENT
    custom_x: float = 10.0
    custom_y: float = 0.0
    monitor_policy: MonitorPolicy = MonitorPolicy.CURSOR

    # Appearance & interaction
    borderless: bool = True  # Always enforced to True by design
    hide_on_click_outside: bool = True
    show_in_taskbar: bool = False
    keep_above: bool = True
    opacity: float = 1.0
    corner_radius: int = 0

    # Animation
    direction: SlideDirection = SlideDirection.TOP
    animation_type: AnimationType = AnimationType.SLIDE_FADE
    animation_scale: float = 1.0

    # Shortcut & script metadata
    shortcut: str = ""
    shortcut_enabled: bool = True
    shortcut_backend_id: str = ""
    script_path: str = ""

    schema_version: int = field(default=SCHEMA_VERSION)

    def __post_init__(self) -> None:
        self.source_type = SourceType(self.source_type)
        self.mode = LifecycleMode(self.mode)
        self.size_unit = SizeUnit(self.size_unit)
        self.position_mode = PositionMode(self.position_mode)
        self.anchor_h = AnchorH(self.anchor_h)
        self.anchor_v = AnchorV(self.anchor_v)
        self.custom_position_unit = SizeUnit(self.custom_position_unit)
        self.monitor_policy = MonitorPolicy(self.monitor_policy)
        self.direction = SlideDirection(self.direction)
        self.animation_type = AnimationType(self.animation_type)

        # Windows managed by SpawnKey must never expose external titlebars/borders.
        self.borderless = True

        # Infer a default match_hint from the executable basename when user enters a raw command.
        if not self.match_hint.strip() and self.exec_cmd.strip():
            try:
                first_token = shlex.split(self.exec_cmd)[0]
            except ValueError:
                first_token = self.exec_cmd.split()[0]
            self.match_hint = Path(os.path.expanduser(first_token)).stem

        if not self.shortcut_backend_id and self.id:
            self.shortcut_backend_id = f"spawnkey-{self.id}.desktop"

        self.validate()

    @property
    def animation_scale_enabled(self) -> bool:
        """Whether the animation scale slider should be active in the UI."""
        return self.animation_type is not AnimationType.NONE

    def validate(self) -> None:
        if not self.id or not _ID_PATTERN.match(self.id):
            raise ValueError(
                f"ID de perfil inválido '{self.id}': usa solo minúsculas, números, '-' o '_'."
            )
        if not self.name or not self.name.strip():
            raise ValueError("El nombre del dropdown no puede estar vacío.")
        if not self.exec_cmd or not self.exec_cmd.strip():
            raise ValueError("El comando o ejecutable no puede estar vacío.")

        if self.size_unit is SizeUnit.PERCENT:
            if not (1.0 <= self.width < 100.0):
                raise ValueError(
                    f"El ancho en porcentaje debe estar entre 1 y 99.9 (menor al monitor), recibido: {self.width}"
                )
            if not (1.0 <= self.height < 100.0):
                raise ValueError(
                    f"El alto en porcentaje debe estar entre 1 y 99.9 (menor al monitor), recibido: {self.height}"
                )
        else:
            if self.width < 50:
                raise ValueError(f"El ancho en píxeles debe ser al menos 50px, recibido: {self.width}")
            if self.height < 50:
                raise ValueError(f"El alto en píxeles debe ser al menos 50px, recibido: {self.height}")

        if self.position_mode is PositionMode.CUSTOM:
            if self.custom_position_unit is SizeUnit.PERCENT:
                if not (0.0 <= self.custom_x < 100.0):
                    raise ValueError(
                        f"La posición X en porcentaje debe estar entre 0 y 99.9, recibido: {self.custom_x}"
                    )
                if not (0.0 <= self.custom_y < 100.0):
                    raise ValueError(
                        f"La posición Y en porcentaje debe estar entre 0 y 99.9, recibido: {self.custom_y}"
                    )
            else:
                if self.custom_x < 0 or self.custom_y < 0:
                    raise ValueError("Las coordenadas personalizadas en píxeles no pueden ser negativas.")

        if not (0.1 <= self.animation_scale <= 5.0):
            raise ValueError(
                f"La escala de animación debe estar entre 0.1 y 5.0, recibido: {self.animation_scale}"
            )

        if not (0.1 <= self.opacity <= 1.0):
            raise ValueError(
                f"La opacidad debe estar entre 0.1 y 1.0, recibido: {self.opacity}"
            )

        if self.corner_radius < 0:
            raise ValueError(
                f"El radio de esquinas no puede ser negativo, recibido: {self.corner_radius}"
            )

        if self.edge_margin < 0:
            raise ValueError(
                f"El margen desde el borde no puede ser negativo, recibido: {self.edge_margin}"
            )

        if not (1000 <= int(self.startup_timeout_ms) <= 60000):
            raise ValueError(
                "El tiempo de espera de arranque (startup_timeout_ms) debe estar entre 1000 y 60000 ms, "
                f"recibido: {self.startup_timeout_ms}"
            )
        self.startup_timeout_ms = int(self.startup_timeout_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "source_type": self.source_type.value,
            "desktop_id": self.desktop_id,
            "exec_cmd": self.exec_cmd,
            "match_hint": self.match_hint,
            "extra_args": self.extra_args,
            "mode": self.mode.value,
            "startup_timeout_ms": self.startup_timeout_ms,
            "size_unit": self.size_unit.value,
            "width": self.width,
            "height": self.height,
            "position_mode": self.position_mode.value,
            "anchor_h": self.anchor_h.value,
            "anchor_v": self.anchor_v.value,
            "edge_margin": self.edge_margin,
            "custom_position_unit": self.custom_position_unit.value,
            "custom_x": self.custom_x,
            "custom_y": self.custom_y,
            "monitor_policy": self.monitor_policy.value,
            "borderless": True,
            "hide_on_click_outside": self.hide_on_click_outside,
            "show_in_taskbar": self.show_in_taskbar,
            "keep_above": self.keep_above,
            "opacity": self.opacity,
            "corner_radius": self.corner_radius,
            "direction": self.direction.value,
            "animation_type": self.animation_type.value,
            "animation_scale": self.animation_scale,
            "shortcut": self.shortcut,
            "shortcut_enabled": self.shortcut_enabled,
            "shortcut_backend_id": self.shortcut_backend_id,
            "script_path": self.script_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DropdownProfile":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            exec_cmd=str(data["exec_cmd"]),
            source_type=SourceType(data.get("source_type", SourceType.EXECUTABLE.value)),
            desktop_id=data.get("desktop_id"),
            match_hint=str(data.get("match_hint", "")),
            extra_args=str(data.get("extra_args", "")),
            mode=LifecycleMode(data.get("mode", LifecycleMode.PERSISTENT.value)),
            startup_timeout_ms=int(data.get("startup_timeout_ms", 8000)),
            size_unit=SizeUnit(data.get("size_unit", SizeUnit.PERCENT.value)),
            width=float(data.get("width", 80.0)),
            height=float(data.get("height", 45.0)),
            position_mode=PositionMode(data.get("position_mode", PositionMode.PRESET.value)),
            anchor_h=AnchorH(data.get("anchor_h", AnchorH.CENTER.value)),
            anchor_v=AnchorV(data.get("anchor_v", AnchorV.TOP.value)),
            edge_margin=int(data.get("edge_margin", 0)),
            custom_position_unit=SizeUnit(
                data.get("custom_position_unit", data.get("size_unit", SizeUnit.PERCENT.value))
            ),
            custom_x=float(data.get("custom_x", 10.0)),
            custom_y=float(data.get("custom_y", 0.0)),
            monitor_policy=MonitorPolicy(data.get("monitor_policy", MonitorPolicy.CURSOR.value)),
            borderless=True,
            hide_on_click_outside=bool(data.get("hide_on_click_outside", True)),
            show_in_taskbar=bool(data.get("show_in_taskbar", False)),
            keep_above=bool(data.get("keep_above", True)),
            opacity=float(data.get("opacity", 1.0)),
            corner_radius=int(data.get("corner_radius", 0)),
            direction=SlideDirection(data.get("direction", SlideDirection.TOP.value)),
            animation_type=AnimationType(data.get("animation_type", AnimationType.SLIDE_FADE.value)),
            animation_scale=float(data.get("animation_scale", 1.0)),
            shortcut=str(data.get("shortcut", "")),
            shortcut_enabled=bool(data.get("shortcut_enabled", True)),
            shortcut_backend_id=str(data.get("shortcut_backend_id", "")),
            script_path=str(data.get("script_path", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class RuntimeState:
    """Volatile runtime state tracked for a single dropdown instance."""

    profile_id: str
    pid: int | None = None
    window_id: str = ""
    visible: bool = False
    watcher_pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "pid": self.pid,
            "window_id": self.window_id,
            "visible": self.visible,
            "watcher_pid": self.watcher_pid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeState":
        return cls(
            profile_id=str(data["profile_id"]),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            window_id=str(data.get("window_id", "")),
            visible=bool(data.get("visible", False)),
            watcher_pid=int(data["watcher_pid"]) if data.get("watcher_pid") is not None else None,
        )
