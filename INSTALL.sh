#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.spawnkey.App"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="${DATA_HOME}/applications"
ICONS_DIR="${DATA_HOME}/icons/hicolor/scalable/apps"

WITH_DESKTOP=0
WITH_KWIN_EFFECT=0
INSTALL_DEPS=0
SHOW_DEPS=0

usage() {
    cat <<'EOF'
Uso: ./INSTALL.sh [opciones]

Opciones:
  --install-deps   Detecta el gestor de paquetes de la distribución e instala dependencias del sistema
  --show-deps      Muestra el comando de dependencias detectado para tu distribución sin ejecutarlo
  --desktop        Crea también un acceso directo en el Escritorio
  --kwin-effect    Compila e instala el micro-efecto opcional de esquinas redondeadas para KDE Plasma 6
  -h, --help       Muestra esta ayuda
EOF
}

for arg in "$@"; do
    case "${arg}" in
        --desktop)
            WITH_DESKTOP=1
            ;;
        --kwin-effect)
            WITH_KWIN_EFFECT=1
            ;;
        --install-deps|--deps)
            INSTALL_DEPS=1
            ;;
        --show-deps)
            SHOW_DEPS=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Opción desconocida: ${arg}" >&2
            usage >&2
            exit 1
            ;;
    esac
done

detect_pkg_command() {
    local with_effect="$1"
    if command -v pacman >/dev/null 2>&1; then
        if [[ "${with_effect}" -eq 1 ]]; then
            echo "sudo pacman -S --needed python python-gobject gtk4 python-dbus base-devel cmake extra-cmake-modules kwin qt6-base"
        else
            echo "sudo pacman -S --needed python python-gobject gtk4 python-dbus"
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        if [[ "${with_effect}" -eq 1 ]]; then
            echo "sudo apt-get update && sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 python3-dbus build-essential cmake extra-cmake-modules kwin-dev qt6-base-dev"
        else
            echo "sudo apt-get update && sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 python3-dbus"
        fi
    elif command -v dnf >/dev/null 2>&1; then
        if [[ "${with_effect}" -eq 1 ]]; then
            echo "sudo dnf install -y python3 python3-gobject gtk4 python3-dbus gcc-c++ cmake extra-cmake-modules kwin-devel qt6-qtbase-devel"
        else
            echo "sudo dnf install -y python3 python3-gobject gtk4 python3-dbus"
        fi
    elif command -v zypper >/dev/null 2>&1; then
        if [[ "${with_effect}" -eq 1 ]]; then
            echo "sudo zypper install -y python3 python3-gobject gtk4 python3-dbus-python gcc-c++ cmake extra-cmake-modules kwin6-devel qt6-base-devel"
        else
            echo "sudo zypper install -y python3 python3-gobject gtk4 python3-dbus-python"
        fi
    elif command -v xbps-install >/dev/null 2>&1; then
        echo "sudo xbps-install -Sy python3 python3-gobject gtk4 python3-dbus"
    elif command -v apk >/dev/null 2>&1; then
        echo "sudo apk add python3 py3-gobject3 gtk4.0 py3-dbus"
    else
        return 1
    fi
}

if [[ "${SHOW_DEPS}" -eq 1 ]]; then
    if CMD="$(detect_pkg_command "${WITH_KWIN_EFFECT}")"; then
        echo "${CMD}"
        exit 0
    else
        echo "No se detectó un gestor de paquetes compatible (pacman, apt-get, dnf, zypper, xbps-install, apk)." >&2
        exit 1
    fi
fi

if [[ "${INSTALL_DEPS}" -eq 1 ]]; then
    if CMD="$(detect_pkg_command "${WITH_KWIN_EFFECT}")"; then
        echo "Instalando dependencias del sistema:"
        echo "  ${CMD}"
        bash -c "${CMD}"
    else
        echo "No se detectó un gestor de paquetes compatible. Instala manualmente Python 3.10+, GTK 4, PyGObject y python-dbus." >&2
        exit 1
    fi
fi

mkdir -p "${APPS_DIR}" "${ICONS_DIR}"
cp "${ROOT_DIR}/assets/spawnkey.svg" "${ICONS_DIR}/${APP_ID}.svg"

DESKTOP_CONTENT="[Desktop Entry]
Type=Application
Name=SpawnKey
GenericName=Administrador de Dropdowns
Comment=Crea ventanas dropdown con atajos de teclado para cualquier aplicación
Exec=${ROOT_DIR}/spawnkey-gui
Icon=${APP_ID}
Terminal=false
Categories=Utility;Settings;System;
Keywords=dropdown;quake;shortcut;atajo;ventana;spawnkey;
StartupWMClass=${APP_ID}
"

printf '%s' "${DESKTOP_CONTENT}" > "${APPS_DIR}/${APP_ID}.desktop"
chmod 644 "${APPS_DIR}/${APP_ID}.desktop"

if [[ "${WITH_DESKTOP}" -eq 1 ]]; then
    DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    if [[ -d "${DESKTOP_DIR}" ]]; then
        printf '%s' "${DESKTOP_CONTENT}" > "${DESKTOP_DIR}/SpawnKey.desktop"
        chmod 755 "${DESKTOP_DIR}/SpawnKey.desktop"
        echo "Acceso directo en escritorio: ${DESKTOP_DIR}/SpawnKey.desktop"
    fi
fi

if [[ "${WITH_KWIN_EFFECT}" -eq 1 ]]; then
    "${ROOT_DIR}/src/spawnkey/backends/kde_wayland/kwin_effect/BUILD.sh"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APPS_DIR}" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${DATA_HOME}/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
fi

echo "Acceso de aplicaciones instalado: ${APPS_DIR}/${APP_ID}.desktop"
