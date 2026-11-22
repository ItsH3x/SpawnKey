#!/usr/bin/env bash
# Compiles and installs the optional SpawnKey KWin 6 rounded-corners effect in user space (~/.local).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-/tmp/spawnkey-kwin-effect-build}"
USER_QT_PLUGINS="${HOME}/.local/lib/qt6/plugins"
TARGET_DIR="${USER_QT_PLUGINS}/kwin/effects/plugins"
ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/plasma-workspace/env"

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
