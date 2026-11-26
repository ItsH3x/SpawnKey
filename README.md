# SpawnKey

Configurador y administrador de ventanas tipo *dropdown* para Linux activadas mediante atajos de teclado globales, diseñado **sin daemon residente**, con prioridad en **Wayland** y orientado a la compatibilidad multi-distribución.

## Características principales

- **Sin daemon residente:** La interfaz gráfica (GTK 4) y la CLI sirven únicamente para configurar perfiles, generar scripts `.sh` autónomos y administrar los atajos globales del sistema. Al pulsar un atajo, el entorno ejecuta directamente el script del perfil y el proceso termina sin dejar servicios residentes en memoria.
- **Control flexible por perfil:**
  - Dimensiones en `%` o `px` siempre estrictamente menores al monitor activo.
  - Posicionamiento por cuadrícula 3x3 (con margen al borde) o coordenadas personalizadas `(X, Y)`.
  - Selección de política de monitor (`cursor`, `focused`, `primary`), reubicación multi-monitor al alternar y vista previa multi-pantalla en la interfaz gráfica.
  - Diálogo de Diagnósticos integrado en la GUI GTK 4 (equivalente visual a `spawnkey doctor`).
  - Internacionalización `gettext` en español e inglés (`LANG` / `LC_MESSAGES`) y metadatos Freedesktop AppStream (`io.github.spawnkey.App.metainfo.xml`).
  - Ventanas sin barra de título ni bordes externos (`borderless`).
  - Animaciones `slide`, `fade`, `slide-fade` o `none`, opacidad por ventana y esquinas redondeadas (en compositores compatibles).
  - Modos de ciclo de vida: `persistent` (minimizar/restaurar), `ephemeral` (abrir/cerrar) y `tray` (aplicaciones de instancia única con bandeja del sistema).
  - Tiempo de espera configurable para aplicaciones con arranque lento (`startup_timeout_ms`).

## Entornos soportados

| Entorno / Backend | Estado | Notas |
| --- | --- | --- |
| KDE Plasma 6 (Wayland) | Activo | Control de ventanas mediante scripts KWin 6 (QML/JS), atajos vía `kglobalaccel` y micro-efecto C++ opcional para esquinas redondeadas. |
| GNOME (Wayland) | Activo | Atajos nativos vía `gsd-media-keys` (`gsettings`) + portal XDG `GlobalShortcuts`; control completo de geometría/estado instalando la extensión incluida (`./INSTALL.sh --gnome-extension`), con fallback de ciclo de vida si no está activa. |
| Hyprland | Activo | Control nativo de ventanas, animaciones, opacidad y redondeo vía `hyprctl`, más atajos en bloque gestionado de configuración o portal XDG. |
| Sway | Activo | Control nativo de ventanas flotantes y scratching/toggle vía `swaymsg`, más atajos en bloque gestionado de configuración o portal XDG. |
| X11 | Activo (fallback universal) | Control EWMH/ICCCM (`xdotool`, `wmctrl`, `xprop`, `xrandr`) y registro de atajos nativo por escritorio (`xfce4`, `mate`, `cinnamon`, etc.) o X11. |

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
./INSTALL.sh --desktop          # Crea además un acceso directo en el Escritorio
./INSTALL.sh --kwin-effect      # Compila e instala el efecto opcional de esquinas redondeadas en KDE 6
./INSTALL.sh --pacman-hook      # (Arch/CachyOS) Compila el efecto e instala un pacman hook para kwin
./INSTALL.sh --gnome-extension  # Instala y habilita la extensión de GNOME Shell (GNOME Wayland)

# Ejecutar la interfaz gráfica directamente desde el repositorio
./spawnkey-gui
```

Para instalar también el comando `spawnkey` en tu entorno Python de usuario:

```bash
pip install --user -e .
```

En **Arch Linux / CachyOS**, puedes construir e instalar el paquete del sistema con `makepkg` usando `packaging/arch/PKGBUILD`:

```bash
makepkg -p packaging/arch/PKGBUILD -si
```

Para desinstalar el acceso directo, el icono y el efecto opcional de KWin:

```bash
./UNINSTALL.sh
```

## Uso rápido por CLI

```bash
# Inspeccionar entorno detectado, capacidades y rutas XDG
PYTHONPATH=src python3 -m spawnkey info

# Diagnosticar entorno, permisos de scripts, atajos huérfanos y colisiones
PYTHONPATH=src python3 -m spawnkey doctor

# Listar aplicaciones .desktop disponibles
PYTHONPATH=src python3 -m spawnkey apps --limit 20

# Crear un perfil desde una app .desktop con atajo global
PYTHONPATH=src python3 -m spawnkey profile create \
  --desktop org.kde.konsole.desktop \
  --mode persistent \
  --width 80 --height 45 \
  --shortcut "Meta+Shift+T"

# Listar perfiles, inspeccionarlos, editarlos y exportar/importar configuraciones portables
PYTHONPATH=src python3 -m spawnkey profile list
PYTHONPATH=src python3 -m spawnkey profile show konsole
PYTHONPATH=src python3 -m spawnkey profile edit konsole --width 75 --opacity 0.95
PYTHONPATH=src python3 -m spawnkey profile export konsole --output konsole.json
PYTHONPATH=src python3 -m spawnkey profile import konsole.json
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
