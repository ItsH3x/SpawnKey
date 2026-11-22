# SpawnKey

Configurador y administrador de ventanas tipo *dropdown* para Linux activadas mediante atajos de teclado globales, diseñado **sin daemon residente**, con prioridad en **Wayland** y orientado a la compatibilidad multi-distribución.

## Características principales

- **Sin daemon residente:** La interfaz gráfica (GTK 4) y la CLI sirven únicamente para configurar perfiles, generar scripts `.sh` autónomos y administrar los atajos globales del sistema. Al pulsar un atajo, el entorno ejecuta directamente el script del perfil y el proceso termina sin dejar servicios residentes en memoria.
- **Control flexible por perfil:**
  - Dimensiones en `%` o `px` siempre estrictamente menores al monitor activo.
  - Posicionamiento por cuadrícula 3x3 (con margen al borde) o coordenadas personalizadas `(X, Y)`.
  - Selección de política de monitor (`cursor`, `focused`, `primary`).
  - Ventanas sin barra de título ni bordes externos (`borderless`).
  - Animaciones `slide`, `fade`, `slide-fade` o `none`, opacidad por ventana y esquinas redondeadas (en compositores compatibles).
  - Modos de ciclo de vida: `persistent` (minimizar/restaurar), `ephemeral` (abrir/cerrar) y `tray` (aplicaciones de instancia única con bandeja del sistema).
  - Tiempo de espera configurable para aplicaciones con arranque lento (`startup_timeout_ms`).

## Entornos soportados

| Entorno / Backend | Estado | Notas |
| --- | --- | --- |
| KDE Plasma 6 (Wayland) | Activo | Control de ventanas mediante scripts KWin 6 (QML/JS), atajos vía `kglobalaccel` y micro-efecto C++ opcional para esquinas redondeadas. |
| GNOME (Wayland) | Soporte inicial | Atajos nativos vía `gsd-media-keys` (`gsettings`) + portal XDG `GlobalShortcuts`; control completo de ventanas sujeto a puente opt-in de GNOME Shell (fallback de ciclo de vida de procesos por defecto). |
| X11 / Hyprland / Sway | En hoja de ruta | Respaldo EWMH/ICCCM en X11 e IPC nativo para Hyprland y Sway. |

## Instalación de dependencias según la distribución

SpawnKey requiere **Python 3.10+**, **GTK 4**, **PyGObject** y **python-dbus** (para la interfaz gráfica y comunicación D-Bus rápida).

### Instalación automática de dependencias

El instalador detecta el gestor de paquetes de tu sistema (`pacman`, `apt-get`, `dnf`, `zypper`, `xbps-install` o `apk`):

```bash
# Instalar dependencias del sistema y registrar SpawnKey en el menú de aplicaciones
./INSTALL.sh --install-deps

# Si usas KDE Plasma 6 Wayland y deseas compilar también el efecto opcional de esquinas redondeadas:
./INSTALL.sh --install-deps --kwin-effect

# Solo ver el comando que corresponde a tu distribución sin ejecutarlo:
./INSTALL.sh --show-deps
```

### Instalación manual por gestor de paquetes

**Arch Linux / CachyOS / EndeavourOS / Manjaro (`pacman`):**

```bash
sudo pacman -S --needed python python-gobject gtk4 python-dbus
# Opcional para compilar el efecto de esquinas redondeadas en KDE Plasma 6:
sudo pacman -S --needed base-devel cmake extra-cmake-modules kwin qt6-base
```

**Debian / Ubuntu / Linux Mint / Pop!_OS (`apt`):**

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 python3-dbus
# Opcional para compilar el efecto de esquinas redondeadas en KDE Plasma 6:
sudo apt-get install -y build-essential cmake extra-cmake-modules kwin-dev qt6-base-dev
```

**Fedora / Nobara (`dnf`):**

```bash
sudo dnf install -y python3 python3-gobject gtk4 python3-dbus
# Opcional para compilar el efecto de esquinas redondeadas en KDE Plasma 6:
sudo dnf install -y gcc-c++ cmake extra-cmake-modules kwin-devel qt6-qtbase-devel
```

**openSUSE Tumbleweed / Leap (`zypper`):**

```bash
sudo zypper install -y python3 python3-gobject gtk4 python3-dbus-python
# Opcional para compilar el efecto de esquinas redondeadas en KDE Plasma 6:
sudo zypper install -y gcc-c++ cmake extra-cmake-modules kwin6-devel qt6-base-devel
```

**Void Linux (`xbps-install`):**

```bash
sudo xbps-install -Sy python3 python3-gobject gtk4 python3-dbus
```

**Alpine Linux (`apk`):**

```bash
sudo apk add python3 py3-gobject3 gtk4.0 py3-dbus
```

## Instalación y ejecución

```bash
# Registrar SpawnKey en el menú de aplicaciones del usuario
./INSTALL.sh

# Opciones adicionales del instalador:
./INSTALL.sh --desktop        # Crea además un acceso directo en el Escritorio
./INSTALL.sh --kwin-effect    # Compila e instala el efecto opcional de esquinas redondeadas en KDE 6

# Ejecutar la interfaz gráfica directamente desde el repositorio
./spawnkey-gui
```

Para instalar también el comando `spawnkey` en tu entorno Python de usuario:

```bash
pip install --user -e .
```

Para desinstalar el acceso directo, el icono y el efecto opcional de KWin:

```bash
./UNINSTALL.sh
```

## Uso rápido por CLI

```bash
# Inspeccionar entorno detectado, capacidades y rutas XDG
PYTHONPATH=src python3 -m spawnkey info

# Listar aplicaciones .desktop disponibles
PYTHONPATH=src python3 -m spawnkey apps --limit 20

# Crear un perfil desde una app .desktop con atajo global
PYTHONPATH=src python3 -m spawnkey profile create \
  --desktop org.kde.konsole.desktop \
  --mode persistent \
  --width 80 --height 45 \
  --shortcut "Meta+Shift+T"

# Listar perfiles y estado real de atajos
PYTHONPATH=src python3 -m spawnkey profile list
PYTHONPATH=src python3 -m spawnkey shortcut list

# Alternar manualmente un perfil (lo mismo que ejecuta su script .sh)
PYTHONPATH=src python3 -m spawnkey run konsole

# Limpiar atajos huérfanos creados por SpawnKey
PYTHONPATH=src python3 -m spawnkey shortcut cleanup
```

## Rutas XDG

- Perfiles JSON: `${XDG_CONFIG_HOME:-~/.config}/spawnkey/profiles/<id>.json`
- Scripts ejecutables: `${XDG_DATA_HOME:-~/.local/share}/spawnkey/scripts/<id>.sh`
- Estado volátil y archivos temporales: `${XDG_RUNTIME_DIR:-/tmp/spawnkey-$UID}/spawnkey/`
- Entradas `.desktop` auxiliares de atajos: `${XDG_DATA_HOME:-~/.local/share}/applications/spawnkey-<id>.desktop`

## Licencia

Distribuido bajo licencia MIT. Consulta [LICENSE](LICENSE).
