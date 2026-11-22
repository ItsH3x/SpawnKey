#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.spawnkey.App"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="${DATA_HOME}/applications"
ICONS_DIR="${DATA_HOME}/icons/hicolor/scalable/apps"
USER_QT_PLUGINS="${HOME}/.local/lib/qt6/plugins"
EFFECT_SO="${USER_QT_PLUGINS}/kwin/effects/plugins/spawnkey_corners.so"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/plasma-workspace/env/spawnkey-qt-plugin-path.sh"

rm -f "${APPS_DIR}/${APP_ID}.desktop"
rm -f "${ICONS_DIR}/${APP_ID}.svg"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
rm -f "${DESKTOP_DIR}/SpawnKey.desktop"

if [[ -f "${EFFECT_SO}" ]]; then
    if command -v qdbus6 >/dev/null 2>&1; then
        qdbus6 org.kde.KWin /Effects org.kde.kwin.Effects.unloadEffect spawnkey_corners >/dev/null 2>&1 || true
    fi
    rm -f "${EFFECT_SO}"
fi
rm -f "${ENV_FILE}"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APPS_DIR}" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${DATA_HOME}/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
fi

echo "SpawnKey desinstalado de ${DATA_HOME}."
