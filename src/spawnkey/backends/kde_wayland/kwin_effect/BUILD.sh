#!/usr/bin/env bash
# Compiles and installs the optional SpawnKey KWin 6 rounded-corners effect in user space (~/.local).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="${USER:-$(id -un)}"
BUILD_DIR="${BUILD_DIR:-/tmp/spawnkey-kwin-effect-build-${CURRENT_USER}}"
USER_QT_PLUGINS="${HOME}/.local/lib/qt6/plugins"
TARGET_DIR="${USER_QT_PLUGINS}/kwin/effects/plugins"
ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/plasma-workspace/env"
INSTALL_PACMAN_HOOK=0

for arg in "$@"; do
    case "${arg}" in
        --install-pacman-hook)
            INSTALL_PACMAN_HOOK=1
            ;;
        -h|--help)
            echo "Uso: $0 [--install-pacman-hook]"
            exit 0
            ;;
        *)
            echo "Opción desconocida: ${arg}" >&2
            exit 1
            ;;
    esac
done

cmake -S "$SCRIPT_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD_DIR" --parallel

mkdir -p "$TARGET_DIR"
cp -f "$BUILD_DIR/bin/kwin/effects/plugins/spawnkey_corners.so" "$TARGET_DIR/spawnkey_corners.so.tmp"
mv -f "$TARGET_DIR/spawnkey_corners.so.tmp" "$TARGET_DIR/spawnkey_corners.so"

# Persist QT_PLUGIN_PATH for future KDE Plasma sessions
mkdir -p "$ENV_DIR"
cat > "$ENV_DIR/spawnkey-qt-plugin-path.sh" <<'EOF'
export QT_PLUGIN_PATH="$HOME/.local/lib/qt6/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
EOF
chmod +x "$ENV_DIR/spawnkey-qt-plugin-path.sh"

if command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file kwinrc --group Plugins --key spawnkey_cornersEnabled true
fi

# If KWin is currently running on D-Bus, register user plugin path and load the effect live
if command -v qdbus6 >/dev/null 2>&1 && qdbus6 org.kde.KWin /Effects org.kde.kwin.Effects.isEffectLoaded spawnkey_corners >/dev/null 2>&1; then
    BOOT_DIR="${XDG_RUNTIME_DIR:-/tmp}/spawnkey/kwin_boot"
    mkdir -p "$BOOT_DIR/bootmod"
    ln -sf "$TARGET_DIR/spawnkey_corners.so" "$BOOT_DIR/bootmod/libspawnkey_corners.so"
    cat > "$BOOT_DIR/bootmod/qmldir" <<'EOF'
module bootmod
plugin spawnkey_corners
EOF
    cat > "$BOOT_DIR/Main.qml" <<'EOF'
import QtQuick
import "bootmod"
Item {}
EOF
    qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript spawnkey_boot_path >/dev/null 2>&1 || true
    SID="$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadDeclarativeScript "$BOOT_DIR/Main.qml" spawnkey_boot_path 2>/dev/null || echo -1)"
    if [ "$SID" -ge 0 ] 2>/dev/null; then
        qdbus6 org.kde.KWin "/Scripting/Script${SID}" org.kde.kwin.Script.run >/dev/null 2>&1 || true
        qdbus6 org.kde.KWin "/Scripting/Script${SID}" org.kde.kwin.Script.stop >/dev/null 2>&1 || true
    fi
    qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript spawnkey_boot_path >/dev/null 2>&1 || true
    rm -rf "$BOOT_DIR"

    qdbus6 org.kde.KWin /Effects org.kde.kwin.Effects.unloadEffect spawnkey_corners >/dev/null 2>&1 || true
    qdbus6 org.kde.KWin /Effects org.kde.kwin.Effects.loadEffect spawnkey_corners >/dev/null 2>&1 || true
fi

echo "Instalado en: $TARGET_DIR/spawnkey_corners.so"

if [[ "${INSTALL_PACMAN_HOOK}" -eq 1 ]]; then
    if ! command -v pacman >/dev/null 2>&1; then
        echo "Aviso: pacman no está disponible en este sistema; se omitió el pacman hook." >&2
    else
        HOOK_PATH="/etc/pacman.d/hooks/spawnkey-kwin-corners.hook"
        HOOK_USER="${SUDO_USER:-$CURRENT_USER}"
        HOOK_HOME="$(eval echo "~${HOOK_USER}")"
        HOOK_CONTENT="[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = kwin

[Action]
Description = Recompilando efecto KWin spawnkey_corners para la nueva versión de KWin...
When = PostTransaction
Exec = /usr/bin/runuser -u ${HOOK_USER} -- /usr/bin/env HOME=${HOOK_HOME} USER=${HOOK_USER} ${SCRIPT_DIR}/BUILD.sh
"
        if [[ "$(id -u)" -eq 0 ]]; then
            mkdir -p /etc/pacman.d/hooks
            printf '%s' "${HOOK_CONTENT}" > "${HOOK_PATH}"
            chmod 644 "${HOOK_PATH}"
        else
            sudo mkdir -p /etc/pacman.d/hooks
            printf '%s' "${HOOK_CONTENT}" | sudo tee "${HOOK_PATH}" >/dev/null
            sudo chmod 644 "${HOOK_PATH}"
        fi
        echo "Pacman hook instalado en: ${HOOK_PATH}"
    fi
fi
