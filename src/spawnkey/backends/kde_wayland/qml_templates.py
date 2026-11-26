"""QML declarative script generator for KDE Plasma 6 KWin window control."""

from __future__ import annotations

import json
from typing import Any

from spawnkey.core.geometry import compute_animation_duration_ms
from spawnkey.core.models import DropdownProfile


def build_kwin_config(
    profile: DropdownProfile,
    *,
    action: str,
    candidate_pids: list[int],
    plugin_name: str,
    expected_window_id: str = "",
    allow_hint_fallback: bool = False,
    saved_original: dict[str, Any] | None = None,
    has_active_watcher: bool = True,
) -> dict[str, Any]:
    """Serialize profile parameters into the JSON config embedded in the QML script."""
    return {
        "action": action,  # "toggle_existing" | "watch_new" | "hide_existing"
        "profileId": profile.id,
        "pluginName": plugin_name,
        "mode": profile.mode.value,
        "startupTimeoutMs": max(1000, int(profile.startup_timeout_ms)),
        "sizeUnit": profile.size_unit.value,
        "width": profile.width,
        "height": profile.height,
        "positionMode": profile.position_mode.value,
        "anchorH": profile.anchor_h.value,
        "anchorV": profile.anchor_v.value,
        "edgeMargin": max(0, int(profile.edge_margin)),
        "customPositionUnit": profile.custom_position_unit.value,
        "customX": profile.custom_x,
        "customY": profile.custom_y,
        "monitorPolicy": profile.monitor_policy.value,
        "relocateOnToggle": bool(getattr(profile, "relocate_on_toggle", True)),
        "borderless": True,
        "hideOnClickOutside": profile.hide_on_click_outside,
        "showInTaskbar": profile.show_in_taskbar,
        "keepAbove": profile.keep_above,
        "opacity": round(float(profile.opacity), 3),
        "cornerRadius": int(profile.corner_radius),
        "direction": profile.direction.value,
        "animationType": profile.animation_type.value,
        "durationMs": compute_animation_duration_ms(profile),
        "matchHint": (profile.match_hint or "").strip().lower(),
        "desktopId": (profile.desktop_id or "").strip().lower(),
        "candidatePids": candidate_pids,
        "expectedWindowId": expected_window_id or "",
        "allowHintFallback": bool(allow_hint_fallback),
        "savedOriginal": saved_original,
        "hasActiveWatcher": bool(has_active_watcher),
    }


def render_kwin_qml(config: dict[str, Any]) -> str:
    """Render a self-contained KWin 6 QML declarative script for a dropdown action."""
    cfg_json = json.dumps(config, ensure_ascii=False)
    # Note: KWin 6 declarative scripts use Workspace.stackingOrder (property)
    # and Workspace.windowAdded / Workspace.windowActivated signals.
    return f"""import QtQuick
import org.kde.kwin

Item {{
    id: root

    readonly property var cfg: ({cfg_json})
    property var trackedWin: null
    property var savedOriginal: cfg.savedOriginal ? cfg.savedOriginal : null
    property var existingIds: ({{}})
    property bool focusArmed: false
    property bool everActivated: false
    property bool animating: false
    property bool shuttingDown: false
    property bool watchingExternalOpen: false
    property bool restoringExternalOpen: false
    property var externalRestoreWin: null
    property int extRestoreElapsed: 0
    property int extRestoreStableTicks: 0
    property real pendingOriginalOpacity: 1.0

    // Geometry confirmation & post-show enforcement state
    property bool waitingForGeometry: false
    property var pendingTargetRect: null
    property var pendingArea: null
    property int geoWaitElapsed: 0
    property int geoStableTicks: 0
    property int trayCloseElapsed: 0
    property bool guardingGeometry: false
    property var guardedTargetRect: null
    property var activeTargetRect: null

    // Animation state
    property real animElapsed: 0
    property real animDuration: 0
    property bool animIsShow: true
    property var animStartRect: null
    property var animEndRect: null
    property real animStartOpacity: 1.0
    property real animEndOpacity: 1.0

    function targetOpacity() {{
        var op = (typeof cfg.opacity === "number") ? cfg.opacity : 1.0;
        return Math.max(0.1, Math.min(1.0, op));
    }}

    function getWindows() {{
        var list = Workspace.stackingOrder;
        return list ? list : [];
    }}

    function normalizeText(v) {{
        return v ? String(v).toLowerCase() : "";
    }}

    function isNormalAppWindow(w) {{
        if (!w) return false;
        if (w.desktopWindow || w.dock || w.splash || w.tooltip || w.notification || w.popupWindow) {{
            return false;
        }}
        if (w.utility || w.toolbar || w.menubar || w.onScreenDisplay || w.criticalNotification || w.appletPopup) {{
            return false;
        }}
        return true;
    }}

    function pidMatches(w) {{
        if (!w || !cfg.candidatePids || cfg.candidatePids.length === 0) return false;
        for (var i = 0; i < cfg.candidatePids.length; i++) {{
            if (w.pid === cfg.candidatePids[i]) return true;
        }}
        return false;
    }}

    function tokenMatches(candidate, hint) {{
        if (!candidate || !hint) return false;
        var normCand = String(candidate).toLowerCase().replace(/_/g, "-");
        var normHint = String(hint).toLowerCase().replace(/_/g, "-");
        if (normCand === normHint) return true;
        if (normCand.indexOf(normHint) !== -1) return true;
        if (normCand.length >= 3 && normHint.indexOf(normCand) !== -1) return true;
        var dashIdx = normHint.indexOf("-");
        if (dashIdx >= 4) {{
            var stem = normHint.slice(0, dashIdx);
            if (normCand === stem || normCand.indexOf(stem) !== -1) return true;
        }}
        if (normHint.indexOf(".") !== -1) {{
            var parts = normHint.split(".");
            for (var p = parts.length - 1; p >= 0; p--) {{
                var seg = parts[p];
                if (seg.length < 4 || seg === "desktop" || seg === "client" || seg === "linux" || seg === "github" || seg === "com" || seg === "org" || seg === "net" || seg === "io" || seg === "app") continue;
                if (normCand === seg || normCand.indexOf(seg) !== -1) return true;
                var segDash = seg.indexOf("-");
                if (segDash >= 4) {{
                    var segStem = seg.slice(0, segDash);
                    if (normCand === segStem || normCand.indexOf(segStem) !== -1) return true;
                }}
            }}
        }}
        return false;
    }}

    function hintMatches(w) {{
        var hint = cfg.matchHint;
        var desk = cfg.desktopId;
        if (desk && desk.endsWith(".desktop")) {{
            desk = desk.slice(0, -8);
        }}
        var rc = normalizeText(w.resourceClass);
        var rn = normalizeText(w.resourceName);
        var df = normalizeText(w.desktopFileName);
        if (df.endsWith(".desktop")) {{
            df = df.slice(0, -8);
        }}

        if (hint) {{
            if (tokenMatches(rc, hint) || tokenMatches(rn, hint) || tokenMatches(df, hint)) {{
                return true;
            }}
        }}
        if (desk) {{
            if (tokenMatches(df, desk) || tokenMatches(rc, desk) || tokenMatches(rn, desk)) {{
                return true;
            }}
        }}
        return false;
    }}

    function findExistingWindow() {{
        var wins = getWindows();
        // Priority 1: exact window internalId tracked in RuntimeState
        if (cfg.expectedWindowId) {{
            for (var e = wins.length - 1; e >= 0; e--) {{
                var we = wins[e];
                if (isNormalAppWindow(we) && String(we.internalId) === cfg.expectedWindowId) {{
                    return we;
                }}
            }}
        }}
        // Priority 2: exact PID from SPAWNKEY_PROFILE_ID process
        for (var i = wins.length - 1; i >= 0; i--) {{
            var w = wins[i];
            if (isNormalAppWindow(w) && pidMatches(w)) {{
                return w;
            }}
        }}
        // Priority 3: previously styled borderless SpawnKey window matching hint
        for (var j = wins.length - 1; j >= 0; j--) {{
            var w2 = wins[j];
            if (isNormalAppWindow(w2) && w2.noBorder && (!cfg.keepAbove || w2.keepAbove) && hintMatches(w2)) {{
                return w2;
            }}
        }}
        // Priority 4: single-instance fallback when launcher delegated to main process and exited
        if (cfg.allowHintFallback) {{
            for (var k = wins.length - 1; k >= 0; k--) {{
                var w3 = wins[k];
                if (isNormalAppWindow(w3) && hintMatches(w3)) {{
                    return w3;
                }}
            }}
        }}
        return null;
    }}

    function getMonitorArea() {{
        var screen = null;
        if (cfg.monitorPolicy === "cursor") {{
            screen = Workspace.screenAt(Workspace.cursorPos);
        }} else if (cfg.monitorPolicy === "primary") {{
            var screens = Workspace.screens;
            if (screens && screens.length > 0) screen = screens[0];
        }} else if (cfg.monitorPolicy === "focused") {{
            if (Workspace.activeWindow && Workspace.activeWindow.output) {{
                screen = Workspace.activeWindow.output;
            }} else {{
                screen = Workspace.activeScreen;
            }}
        }}
        if (!screen) {{
            screen = Workspace.activeScreen;
        }}
        var area = Workspace.clientArea(KWin.MaximizeArea, screen, Workspace.currentDesktop);
        if (!area || area.width <= 1 || area.height <= 1) {{
            area = screen.geometry;
        }}
        return area;
    }}

    function isWindowOnArea(w, area) {{
        if (!w || !w.frameGeometry || !area) return true;
        var fg = w.frameGeometry;
        var cx = fg.x + fg.width / 2.0;
        var cy = fg.y + fg.height / 2.0;
        return cx >= area.x && cx < (area.x + area.width) && cy >= area.y && cy < (area.y + area.height);
    }}

    function shouldRelocateOrFocus(w) {{
        if (!w) return false;
        if (cfg.relocateOnToggle === false) return false;
        if (cfg.monitorPolicy !== "cursor" && cfg.monitorPolicy !== "focused") return false;
        return !isWindowOnArea(w, getMonitorArea()) || !w.active;
    }}

    function clampDim(val, limit) {{
        var maxAllowed = Math.max(1, Math.floor(limit) - 1);
        var minAllowed = Math.min(50, maxAllowed);
        return Math.max(minAllowed, Math.min(Math.round(val), maxAllowed));
    }}

    function computeTargetRect(area) {{
        var rawW = cfg.sizeUnit === "percent" ? (area.width * (cfg.width / 100.0)) : cfg.width;
        var rawH = cfg.sizeUnit === "percent" ? (area.height * (cfg.height / 100.0)) : cfg.height;
        var w = clampDim(rawW, area.width);
        var h = clampDim(rawH, area.height);

        var relX = 0;
        var relY = 0;
        if (cfg.positionMode === "preset") {{
            var margin = Math.max(0, Math.round(cfg.edgeMargin || 0));
            if (cfg.anchorH === "left") relX = margin;
            else if (cfg.anchorH === "center") relX = Math.floor((area.width - w) / 2);
            else relX = Math.floor(area.width - w - margin);

            if (cfg.anchorV === "top") relY = margin;
            else if (cfg.anchorV === "center") relY = Math.floor((area.height - h) / 2);
            else relY = Math.floor(area.height - h - margin);
        }} else {{
            relX = cfg.customPositionUnit === "percent"
                ? Math.round(area.width * (cfg.customX / 100.0))
                : Math.round(cfg.customX);
            relY = cfg.customPositionUnit === "percent"
                ? Math.round(area.height * (cfg.customY / 100.0))
                : Math.round(cfg.customY);
        }}

        var maxRelX = Math.max(0, Math.floor(area.width - w));
        var maxRelY = Math.max(0, Math.floor(area.height - h));
        relX = Math.max(0, Math.min(relX, maxRelX));
        relY = Math.max(0, Math.min(relY, maxRelY));

        return {{
            x: Math.round(area.x + relX),
            y: Math.round(area.y + relY),
            width: w,
            height: h
        }};
    }}

    function computeSlideRect(target, area) {{
        // Offset by window size along the entry edge for a clean glide animation.
        var sx = target.x;
        var sy = target.y;
        var stepX = Math.min(target.width, Math.max(120, Math.round(area.width * 0.35)));
        var stepY = Math.min(target.height, Math.max(120, Math.round(area.height * 0.35)));
        if (cfg.direction === "top") {{
            sy = Math.round(area.y - stepY);
        }} else if (cfg.direction === "bottom") {{
            sy = Math.round(area.y + area.height - target.height + stepY);
        }} else if (cfg.direction === "left") {{
            sx = Math.round(area.x - stepX);
        }} else {{
            sx = Math.round(area.x + area.width - target.width + stepX);
        }}
        return {{
            x: sx,
            y: sy,
            width: target.width,
            height: target.height
        }};
    }}

    function isMaximizedOrFullscreen(w) {{
        if (!w) return false;
        if (w.fullScreen) return true;
        if (typeof w.maximizeMode === "number" && w.maximizeMode !== 0) return true;
        return false;
    }}

    function unmaximizeIfNeeded(w, restoreRect) {{
        if (!w) return;
        if (w.fullScreen) {{
            w.fullScreen = false;
        }}
        if (w.setMaximize) {{
            if (restoreRect) {{
                w.setMaximize(
                    false,
                    false,
                    Qt.rect(restoreRect.x, restoreRect.y, restoreRect.width, restoreRect.height)
                );
            }} else if (typeof w.maximizeMode === "number" && w.maximizeMode !== 0) {{
                w.setMaximize(false, false);
            }}
        }}
    }}

    function applyBaseFlags(w) {{
        w.noBorder = true;
        w.keepBelow = false;
        w.keepAbove = cfg.keepAbove;
        w.skipTaskbar = !cfg.showInTaskbar;
        w.skipPager = !cfg.showInTaskbar;
        w.skipSwitcher = !cfg.showInTaskbar;
        w.onAllDesktops = true;
    }}

    function setGeometry(w, rect) {{
        w.frameGeometry = Qt.rect(rect.x, rect.y, rect.width, rect.height);
    }}

    function isGeometrySettled(w, target) {{
        if (!w || !target) return false;
        if (isMaximizedOrFullscreen(w)) return false;
        var fg = w.frameGeometry;
        if (!fg) return false;
        if (Math.abs(fg.width - target.width) > 4 || Math.abs(fg.height - target.height) > 4) {{
            return false;
        }}
        var cg = w.clientGeometry;
        if (cg && cg.width > 1 && cg.height > 1) {{
            if (Math.abs(cg.width - target.width) > 4 || Math.abs(cg.height - target.height) > 4) {{
                return false;
            }}
        }}
        return true;
    }}

    function isRectAligned(w, target) {{
        if (!w || !target) return false;
        if (isMaximizedOrFullscreen(w)) return false;
        var fg = w.frameGeometry;
        if (!fg) return false;
        return Math.abs(fg.x - target.x) <= 4
            && Math.abs(fg.y - target.y) <= 4
            && Math.abs(fg.width - target.width) <= 4
            && Math.abs(fg.height - target.height) <= 4;
    }}

    function startPostShowGuard(target) {{
        guardedTargetRect = target;
        guardingGeometry = true;
        postShowGuardTimer.restart();
        postShowCheckTimer.restart();
    }}

    function enforceGuardedGeometry() {{
        if (!guardingGeometry || !trackedWin || !guardedTargetRect || animating || shuttingDown || trackedWin.minimized) {{
            return;
        }}
        if (!isRectAligned(trackedWin, guardedTargetRect)) {{
            unmaximizeIfNeeded(trackedWin, guardedTargetRect);
            applyBaseFlags(trackedWin);
            setGeometry(trackedWin, guardedTargetRect);
        }}
    }}

    function captureOriginalState(w) {{
        if (!w) return;
        if (w.noBorder) return;
        // If we already captured a valid non-dropdown geometry earlier, preserve it.
        if (savedOriginal && (
            savedOriginal.fullScreen
            || (savedOriginal.maximizeMode && savedOriginal.maximizeMode !== 0)
            || (savedOriginal.width > 50 && savedOriginal.height > 50)
        )) {{
            savedOriginal.noBorder = false;
            return;
        }}
        var origOpacity = (typeof w.opacity === "number" && w.opacity > 0.0)
            ? w.opacity
            : pendingOriginalOpacity;
        savedOriginal = {{
            x: Math.round(w.frameGeometry.x),
            y: Math.round(w.frameGeometry.y),
            width: Math.round(w.frameGeometry.width),
            height: Math.round(w.frameGeometry.height),
            noBorder: false,
            keepAbove: Boolean(w.keepAbove),
            keepBelow: Boolean(w.keepBelow),
            skipTaskbar: Boolean(w.skipTaskbar),
            skipPager: Boolean(w.skipPager),
            skipSwitcher: Boolean(w.skipSwitcher),
            onAllDesktops: Boolean(w.onAllDesktops),
            maximizeMode: (typeof w.maximizeMode === "number" ? w.maximizeMode : 0),
            fullScreen: Boolean(w.fullScreen),
            skipsCloseAnimation: Boolean(w.skipsCloseAnimation),
            opacity: origOpacity
        }};
    }}

    function restoreOriginalState(w, forceGeometry) {{
        if (!w || !savedOriginal) return;
        w.noBorder = false;
        w.keepBelow = Boolean(savedOriginal.keepBelow);
        w.keepAbove = Boolean(savedOriginal.keepAbove);
        w.skipTaskbar = Boolean(savedOriginal.skipTaskbar);
        w.skipPager = Boolean(savedOriginal.skipPager);
        w.skipSwitcher = Boolean(savedOriginal.skipSwitcher);
        w.onAllDesktops = Boolean(savedOriginal.onAllDesktops);
        if (forceGeometry || savedOriginal.restoreGeometry) {{
            if (savedOriginal.fullScreen) {{
                w.fullScreen = true;
                return;
            }}
            if (savedOriginal.maximizeMode && savedOriginal.maximizeMode !== 0 && w.setMaximize) {{
                var vert = (savedOriginal.maximizeMode & 1) !== 0;
                var horiz = (savedOriginal.maximizeMode & 2) !== 0;
                w.setMaximize(vert, horiz);
                return;
            }}
            if (savedOriginal.width > 50 && savedOriginal.height > 50) {{
                unmaximizeIfNeeded(w, savedOriginal);
                setGeometry(w, savedOriginal);
            }}
        }}
    }}

    function isExternalRestoreSettled(w) {{
        if (!w) return true;
        if (w.noBorder) return false;
        if (!savedOriginal) return true;
        if (savedOriginal.fullScreen) {{
            return Boolean(w.fullScreen);
        }}
        if (savedOriginal.maximizeMode && savedOriginal.maximizeMode !== 0) {{
            return typeof w.maximizeMode === "number" && w.maximizeMode === savedOriginal.maximizeMode;
        }}
        if (savedOriginal.width > 50 && savedOriginal.height > 50) {{
            if (isMaximizedOrFullscreen(w)) return false;
            var fg = w.frameGeometry;
            if (!fg) return false;
            return Math.abs(fg.x - savedOriginal.x) <= 4
                && Math.abs(fg.y - savedOriginal.y) <= 4
                && Math.abs(fg.width - savedOriginal.width) <= 4
                && Math.abs(fg.height - savedOriginal.height) <= 4;
        }}
        return true;
    }}

    function scheduleUnload() {{
        shuttingDown = true;
        waitingForGeometry = false;
        guardingGeometry = false;
        geoConfirmTimer.stop();
        postShowGuardTimer.stop();
        postShowCheckTimer.stop();
    }}

    function finishHide(w) {{
        animating = false;
        focusArmed = false;
        guardingGeometry = false;
        postShowGuardTimer.stop();
        postShowCheckTimer.stop();
        if (!w) {{
            scheduleUnload();
            return;
        }}
        if (cfg.mode === "tray") {{
            shuttingDown = true;
            w.opacity = 0.0;
            w.skipsCloseAnimation = true;
            restoreOriginalState(w);
            trayCloseElapsed = 0;
            trayCloseTimer.restart();
            return;
        }}
        if (cfg.mode === "ephemeral") {{
            if (cfg.animationType !== "none" && cfg.durationMs > 0) {{
                w.opacity = 0.0;
                w.skipsCloseAnimation = true;
            }} else {{
                w.opacity = targetOpacity();
            }}
            w.closeWindow();
            scheduleUnload();
        }} else {{
            w.minimized = true;
            w.opacity = targetOpacity();
            shuttingDown = true;
        }}
    }}

    function focusWindow(w) {{
        if (!w || w.minimized) return;
        if (Workspace.raiseWindow) {{
            Workspace.raiseWindow(w);
        }}
        Workspace.activeWindow = w;
        if (w.active) {{
            everActivated = true;
        }}
    }}

    function beginShowAnimation(w, target, area) {{
        if (!w || shuttingDown) return;
        waitingForGeometry = false;
        geoConfirmTimer.stop();
        activeTargetRect = target;
        unmaximizeIfNeeded(w, target);
        applyBaseFlags(w);

        var desiredOpacity = targetOpacity();
        var doSlide = (cfg.animationType === "slide" || cfg.animationType === "slide-fade");
        var doFade = (cfg.animationType === "fade" || cfg.animationType === "slide-fade");

        if (cfg.animationType === "none" || cfg.durationMs <= 0) {{
            setGeometry(w, target);
            w.opacity = desiredOpacity;
            focusWindow(w);
            armFocusWatcher();
            startPostShowGuard(target);
            return;
        }}

        animStartRect = doSlide ? computeSlideRect(target, area) : target;
        animEndRect = target;
        animStartOpacity = doFade ? 0.0 : desiredOpacity;
        animEndOpacity = desiredOpacity;
        animDuration = cfg.durationMs;
        animElapsed = 0;
        animIsShow = true;
        animating = true;

        setGeometry(w, animStartRect);
        w.opacity = animStartOpacity;
        focusWindow(w);
        animTimer.start();
    }}

    function showWindow(w, requireConfirm) {{
        if (!w) return;
        trackedWin = w;
        focusArmed = false;
        everActivated = false;
        guardingGeometry = false;
        postShowGuardTimer.stop();
        postShowCheckTimer.stop();

        var area = getMonitorArea();
        var target = computeTargetRect(area);
        activeTargetRect = target;
        var alreadySettled = isGeometrySettled(w, target);

        if (w.minimized) {{
            w.minimized = false;
        }}

        unmaximizeIfNeeded(w, target);
        applyBaseFlags(w);
        setGeometry(w, target);

        if ((!requireConfirm || alreadySettled) && isGeometrySettled(w, target)) {{
            beginShowAnimation(w, target, area);
            return;
        }}

        // Keep window hidden at opacity 0.0 until unmaximize + frameGeometry/clientGeometry settle
        w.opacity = 0.0;
        waitingForGeometry = true;
        pendingTargetRect = target;
        pendingArea = area;
        geoWaitElapsed = 0;
        geoStableTicks = 0;
        geoConfirmTimer.restart();
    }}

    function cancelPendingWindow(w) {{
        if (!w || shuttingDown) return;
        trackedWin = w;
        watchTimeoutTimer.stop();
        waitingForGeometry = false;
        geoConfirmTimer.stop();
        w.opacity = 0.0;
        if (cfg.mode === "tray") {{
            w.skipsCloseAnimation = true;
            if (w.noBorder) {{
                restoreOriginalState(w);
            }}
            shuttingDown = true;
            trayCloseTimer.restart();
        }} else if (cfg.mode === "ephemeral") {{
            w.skipsCloseAnimation = true;
            w.closeWindow();
            scheduleUnload();
        }} else {{
            w.minimized = true;
            w.opacity = targetOpacity();
            scheduleUnload();
        }}
    }}

    function hideOrCloseWindow(w) {{
        if (!w || shuttingDown) return;
        trackedWin = w;
        focusArmed = false;
        waitingForGeometry = false;
        guardingGeometry = false;
        geoConfirmTimer.stop();
        postShowGuardTimer.stop();
        postShowCheckTimer.stop();

        var doSlide = (cfg.animationType === "slide" || cfg.animationType === "slide-fade");
        var doFade = (cfg.animationType === "fade" || cfg.animationType === "slide-fade");

        if (cfg.animationType === "none" || cfg.durationMs <= 0) {{
            finishHide(w);
            return;
        }}

        var area = getMonitorArea();
        var current = {{
            x: Math.round(w.frameGeometry.x),
            y: Math.round(w.frameGeometry.y),
            width: Math.round(w.frameGeometry.width),
            height: Math.round(w.frameGeometry.height)
        }};
        var startOp = (typeof w.opacity === "number" && w.opacity > 0.0) ? w.opacity : targetOpacity();
        animStartRect = current;
        animEndRect = doSlide ? computeSlideRect(current, area) : current;
        animStartOpacity = startOp;
        animEndOpacity = doFade ? 0.0 : startOp;
        animDuration = Math.max(45, Math.round(cfg.durationMs * 0.65));
        animElapsed = 0;
        animIsShow = false;
        animating = true;
        animTimer.start();
    }}

    function armFocusWatcher() {{
        if (!cfg.hideOnClickOutside) return;
        if (trackedWin && trackedWin.active) {{
            everActivated = true;
            focusArmed = true;
            return;
        }}
        focusArmTimer.restart();
    }}

    function isSameWindow(a, b) {{
        if (!a || !b) return false;
        return String(a.internalId) === String(b.internalId);
    }}

    function isChildOrTransientWindow(win, parentWin) {{
        if (!win || !parentWin) return false;
        var cur = win;
        var depth = 0;
        while (cur && depth < 8) {{
            if (isSameWindow(cur, parentWin)) return true;
            cur = cur.transientFor;
            depth += 1;
        }}
        return false;
    }}

    function handlePotentialFocusLoss(activeWin) {{
        if (!focusArmed || !trackedWin || animating || waitingForGeometry || shuttingDown) return;
        if (!cfg.hideOnClickOutside) return;
        if (trackedWin.minimized) return;
        if (activeWin && isChildOrTransientWindow(activeWin, trackedWin)) {{
            return;
        }}
        if (!activeWin || !isSameWindow(activeWin, trackedWin) || !trackedWin.active) {{
            hideOrCloseWindow(trackedWin);
        }}
    }}

    Timer {{
        id: geoConfirmTimer
        interval: 12
        repeat: true
        running: false
        onTriggered: {{
            if (!waitingForGeometry || !trackedWin || !pendingTargetRect || shuttingDown) {{
                geoConfirmTimer.stop();
                waitingForGeometry = false;
                return;
            }}
            geoWaitElapsed += interval;
            if (isMaximizedOrFullscreen(trackedWin)) {{
                unmaximizeIfNeeded(trackedWin, pendingTargetRect);
                geoStableTicks = 0;
            }}
            if (!isGeometrySettled(trackedWin, pendingTargetRect)) {{
                unmaximizeIfNeeded(trackedWin, pendingTargetRect);
                setGeometry(trackedWin, pendingTargetRect);
                geoStableTicks = 0;
            }} else {{
                geoStableTicks += 1;
            }}
            if (geoStableTicks >= 2 || geoWaitElapsed >= 200) {{
                beginShowAnimation(trackedWin, pendingTargetRect, pendingArea);
            }}
        }}
    }}

    Timer {{
        id: postShowCheckTimer
        interval: 60
        repeat: true
        running: false
        onTriggered: {{
            enforceGuardedGeometry();
        }}
    }}

    Timer {{
        id: postShowGuardTimer
        interval: 420
        repeat: false
        running: false
        onTriggered: {{
            guardingGeometry = false;
            postShowCheckTimer.stop();
        }}
    }}

    Timer {{
        id: animTimer
        interval: 16
        repeat: true
        running: false
        onTriggered: {{
            if (!trackedWin) {{
                animTimer.stop();
                animating = false;
                return;
            }}
            animElapsed += interval;
            var rawT = Math.min(1.0, animElapsed / Math.max(1, animDuration));
            var t = animIsShow
                ? (1.0 - Math.pow(1.0 - rawT, 3))
                : Math.pow(rawT, 2);

            var rx = Math.round(animStartRect.x + (animEndRect.x - animStartRect.x) * t);
            var ry = Math.round(animStartRect.y + (animEndRect.y - animStartRect.y) * t);
            if (animIsShow && isMaximizedOrFullscreen(trackedWin)) {{
                unmaximizeIfNeeded(trackedWin, animEndRect);
                applyBaseFlags(trackedWin);
            }}
            setGeometry(trackedWin, {{
                x: rx,
                y: ry,
                width: animEndRect.width,
                height: animEndRect.height
            }});

            var op = animStartOpacity + (animEndOpacity - animStartOpacity) * t;
            trackedWin.opacity = Math.max(0.0, Math.min(1.0, op));

            if (rawT >= 1.0) {{
                animTimer.stop();
                animating = false;
                if (animIsShow) {{
                    unmaximizeIfNeeded(trackedWin, animEndRect);
                    applyBaseFlags(trackedWin);
                    setGeometry(trackedWin, animEndRect);
                    trackedWin.opacity = targetOpacity();
                    focusWindow(trackedWin);
                    armFocusWatcher();
                    startPostShowGuard(animEndRect);
                }} else {{
                    finishHide(trackedWin);
                }}
            }}
        }}
    }}

    Timer {{
        id: focusArmTimer
        interval: 100
        repeat: false
        running: false
        onTriggered: {{
            if (!trackedWin || trackedWin.minimized || shuttingDown) return;
            if (!trackedWin.active && !everActivated) {{
                // Retry activation once in case Wayland surface wasn't ready yet
                focusWindow(trackedWin);
                focusArmed = true;
                return;
            }}
            focusArmed = true;
            if (!trackedWin.active) {{
                handlePotentialFocusLoss(Workspace.activeWindow);
            }}
        }}
    }}

    Timer {{
        id: settleNewWindowTimer
        interval: 10
        repeat: false
        running: false
        onTriggered: {{
            if (trackedWin) {{
                if (cfg.mode === "tray") {{
                    captureOriginalState(trackedWin);
                }}
                showWindow(trackedWin, true);
            }}
        }}
    }}

    Timer {{
        id: trayCloseTimer
        interval: 15
        repeat: false
        running: false
        onTriggered: {{
            if (trackedWin) {{
                trackedWin.opacity = 0.0;
                trackedWin.skipsCloseAnimation = true;
                trackedWin.closeWindow();
                trayPostCloseTimer.restart();
            }} else {{
                scheduleUnload();
            }}
        }}
    }}

    Timer {{
        id: trayPostCloseTimer
        interval: 600
        repeat: false
        running: false
        onTriggered: {{
            // Safety fallback: only reached if the app refused/cancelled close and stayed mapped.
            if (trackedWin) {{
                var origOpacity = (savedOriginal && typeof savedOriginal.opacity === "number")
                    ? savedOriginal.opacity
                    : 1.0;
                var origSkipsClose = (savedOriginal && typeof savedOriginal.skipsCloseAnimation === "boolean")
                    ? savedOriginal.skipsCloseAnimation
                    : false;
                trackedWin.skipsCloseAnimation = origSkipsClose;
                trackedWin.opacity = origOpacity;
            }}
            scheduleUnload();
        }}
    }}

    Timer {{
        id: externalRestoreTimer
        interval: 15
        repeat: true
        running: false
        onTriggered: {{
            if (!restoringExternalOpen || !externalRestoreWin) {{
                externalRestoreTimer.stop();
                restoringExternalOpen = false;
                externalRestoreWin = null;
                return;
            }}
            extRestoreElapsed += interval;
            if (isExternalRestoreSettled(externalRestoreWin)) {{
                extRestoreStableTicks += 1;
            }} else {{
                restoreOriginalState(externalRestoreWin, true);
                extRestoreStableTicks = 0;
            }}
            if (extRestoreStableTicks >= 2 || extRestoreElapsed >= 150) {{
                externalRestoreTimer.stop();
                restoreOriginalState(externalRestoreWin, true);
                externalRestoreWin.skipsCloseAnimation = Boolean(savedOriginal && savedOriginal.skipsCloseAnimation);
                externalRestoreWin.opacity = (savedOriginal && typeof savedOriginal.opacity === "number" && savedOriginal.opacity > 0.0)
                    ? savedOriginal.opacity
                    : 1.0;
                focusWindow(externalRestoreWin);
                restoringExternalOpen = false;
                externalRestoreWin = null;
                scheduleUnload();
            }}
        }}
    }}

    Timer {{
        id: watchTimeoutTimer
        interval: (typeof cfg.startupTimeoutMs === "number" && cfg.startupTimeoutMs >= 1000) ? cfg.startupTimeoutMs : 8000
        repeat: false
        running: false
        onTriggered: {{
            if (!trackedWin) {{
                scheduleUnload();
            }}
        }}
    }}

    Connections {{
        target: root.trackedWin
        enabled: root.trackedWin !== null
        ignoreUnknownSignals: true

        function onActiveChanged() {{
            if (!trackedWin || shuttingDown) return;
            if (trackedWin.active) {{
                everActivated = true;
                if (!animating && !waitingForGeometry && cfg.hideOnClickOutside) {{
                    focusArmed = true;
                }}
                return;
            }}
            handlePotentialFocusLoss(Workspace.activeWindow);
        }}

        function onKeepBelowChanged() {{
            if (cfg.mode !== "tray" || !trackedWin) return;
            // Remote toggle nudge sent by SpawnKey when user repeats the shortcut
            if (trackedWin.keepBelow) {{
                trackedWin.keepBelow = false;
                trackedWin.keepAbove = cfg.keepAbove;
                // If currently closing or restoring before close, reverse immediately into show!
                if ((animating && !animIsShow) || trayCloseTimer.running || shuttingDown) {{
                    animTimer.stop();
                    trayCloseTimer.stop();
                    trayPostCloseTimer.stop();
                    animating = false;
                    shuttingDown = false;
                    showWindow(trackedWin, true);
                    return;
                }}
                if (animating && animIsShow) {{
                    animTimer.stop();
                    animating = false;
                }}
                if (waitingForGeometry) {{
                    waitingForGeometry = false;
                    geoConfirmTimer.stop();
                    finishHide(trackedWin);
                    return;
                }}
                hideOrCloseWindow(trackedWin);
            }}
        }}

        function onFrameGeometryChanged() {{
            if (!trackedWin || shuttingDown) return;
            if (waitingForGeometry && pendingTargetRect) {{
                if (!isGeometrySettled(trackedWin, pendingTargetRect)) {{
                    geoStableTicks = 0;
                }}
                return;
            }}
            if (guardingGeometry) {{
                enforceGuardedGeometry();
            }}
        }}

        function onClientGeometryChanged() {{
            if (!trackedWin || shuttingDown) return;
            if (waitingForGeometry && pendingTargetRect) {{
                if (!isGeometrySettled(trackedWin, pendingTargetRect)) {{
                    geoStableTicks = 0;
                }}
                return;
            }}
            if (guardingGeometry) {{
                enforceGuardedGeometry();
            }}
        }}

        function onMaximizedChanged() {{
            if (!trackedWin || shuttingDown || trackedWin.minimized) return;
            if (waitingForGeometry && pendingTargetRect) {{
                geoStableTicks = 0;
                if (isMaximizedOrFullscreen(trackedWin)) {{
                    unmaximizeIfNeeded(trackedWin, pendingTargetRect);
                    setGeometry(trackedWin, pendingTargetRect);
                }}
                return;
            }}
            if (isMaximizedOrFullscreen(trackedWin) && activeTargetRect && (!animating || animIsShow)) {{
                unmaximizeIfNeeded(trackedWin, activeTargetRect);
                applyBaseFlags(trackedWin);
                if (!animating) {{
                    setGeometry(trackedWin, activeTargetRect);
                }}
                return;
            }}
            if (guardingGeometry) {{
                enforceGuardedGeometry();
            }}
        }}
    }}

    Connections {{
        target: Workspace

        function onWindowAdded(w) {{
            if (watchingExternalOpen && !restoringExternalOpen) {{
                if (isNormalAppWindow(w) && !w.transientFor && hintMatches(w)) {{
                    watchingExternalOpen = false;
                    restoringExternalOpen = true;
                    externalRestoreWin = w;
                    extRestoreElapsed = 0;
                    extRestoreStableTicks = 0;
                    w.opacity = 0.0;
                    w.skipsCloseAnimation = true;
                    restoreOriginalState(w, true);
                    externalRestoreTimer.restart();
                }}
                return;
            }}
            if (restoringExternalOpen) return;
            if ((cfg.action !== "watch_new" && cfg.action !== "cancel_pending") || trackedWin || shuttingDown) return;
            if (!isNormalAppWindow(w)) return;
            if (w.transientFor) return;
            if (existingIds[String(w.internalId)]) return;

            if (pidMatches(w) || hintMatches(w) || (!cfg.matchHint && !cfg.desktopId)) {{
                if (cfg.action === "cancel_pending") {{
                    cancelPendingWindow(w);
                    return;
                }}
                trackedWin = w;
                watchTimeoutTimer.stop();
                // Hide immediately for ALL modes and ALL animation types while settling geometry
                pendingOriginalOpacity = (typeof w.opacity === "number" && w.opacity > 0.0)
                    ? w.opacity
                    : 1.0;
                w.opacity = 0.0;
                if (w.frameGeometry && w.frameGeometry.width > 1 && w.frameGeometry.height > 1) {{
                    if (cfg.mode === "tray") {{
                        captureOriginalState(w);
                    }} else {{
                        applyBaseFlags(w);
                    }}
                    showWindow(w, true);
                    return;
                }}
                if (cfg.mode !== "tray") {{
                    applyBaseFlags(w);
                }}
                settleNewWindowTimer.restart();
            }}
        }}

        function onWindowActivated(w) {{
            if (trackedWin && w && isSameWindow(w, trackedWin)) {{
                everActivated = true;
                if (!animating && !waitingForGeometry && cfg.hideOnClickOutside) {{
                    focusArmed = true;
                }}
                return;
            }}
            handlePotentialFocusLoss(w);
        }}

        function onWindowRemoved(w) {{
            if (externalRestoreWin && isSameWindow(w, externalRestoreWin)) {{
                externalRestoreTimer.stop();
                restoringExternalOpen = false;
                externalRestoreWin = null;
                scheduleUnload();
                return;
            }}
            if (trackedWin && isSameWindow(w, trackedWin)) {{
                var wasShuttingDown = shuttingDown;
                if (cfg.mode === "tray" && !wasShuttingDown) {{
                    restoreOriginalState(trackedWin, false);
                }}
                animTimer.stop();
                geoConfirmTimer.stop();
                postShowGuardTimer.stop();
                postShowCheckTimer.stop();
                trayCloseTimer.stop();
                trayPostCloseTimer.stop();
                focusArmTimer.stop();
                settleNewWindowTimer.stop();
                watchTimeoutTimer.stop();
                externalRestoreTimer.stop();
                trackedWin = null;
                focusArmed = false;
                if (cfg.mode === "tray" && wasShuttingDown && cfg.action !== "cancel_pending") {{
                    shuttingDown = false;
                    watchingExternalOpen = true;
                }} else {{
                    scheduleUnload();
                }}
            }}
        }}
    }}

    Component.onCompleted: {{
        var wins = getWindows();
        for (var i = 0; i < wins.length; i++) {{
            existingIds[String(wins[i].internalId)] = true;
        }}

        if (cfg.action === "toggle_existing") {{
            var existing = findExistingWindow();
            if (existing) {{
                if (existing.minimized || shouldRelocateOrFocus(existing)) {{
                    showWindow(existing, false);
                }} else {{
                    hideOrCloseWindow(existing);
                }}
            }}
        }} else if (cfg.action === "hide_existing") {{
            var toHide = findExistingWindow();
            if (toHide && !toHide.minimized) {{
                hideOrCloseWindow(toHide);
            }}
        }} else if (cfg.action === "cancel_pending") {{
            var pending = findExistingWindow();
            if (pending && !pending.minimized) {{
                cancelPendingWindow(pending);
                return;
            }}
            watchTimeoutTimer.interval = Math.min(4000, watchTimeoutTimer.interval);
            watchTimeoutTimer.start();
        }} else if (cfg.action === "watch_new") {{
            // In tray mode, if the app window is already open on another monitor,
            // capture its original state and bring it in as dropdown immediately.
            if (cfg.mode === "tray") {{
                for (var k = wins.length - 1; k >= 0; k--) {{
                    var cand = wins[k];
                    if (isNormalAppWindow(cand) && hintMatches(cand)) {{
                        trackedWin = cand;
                        pendingOriginalOpacity = (typeof cand.opacity === "number" && cand.opacity > 0.0)
                            ? cand.opacity
                            : 1.0;
                        captureOriginalState(cand);
                        cand.opacity = 0.0;
                        showWindow(cand, true);
                        return;
                    }}
                }}
            }}
            watchTimeoutTimer.start();
        }}
    }}
}}
"""


def render_tray_probe_and_dismiss_js(
    config: dict[str, Any],
    flag_plugin_name: str,
    normal_flag_name: str = "",
    done_flag_name: str = "",
) -> str:
    """Render a KWin 6 JS script that probes a tray app window and dismisses it if open as dropdown."""
    cfg_json = json.dumps(config, ensure_ascii=False)
    flag_json = json.dumps(flag_plugin_name, ensure_ascii=False)
    normal_json = json.dumps(normal_flag_name, ensure_ascii=False)
    done_json = json.dumps(done_flag_name, ensure_ascii=False)
    return f"""var cfg = {cfg_json};
var flagName = {flag_json};
var normalFlagName = {normal_json};
var doneFlagName = {done_json};

function normalizeText(v) {{
    return v ? String(v).toLowerCase() : "";
}}

function tokenMatches(candidate, hint) {{
    if (!candidate || !hint) return false;
    var normCand = String(candidate).toLowerCase().replace(/_/g, "-");
    var normHint = String(hint).toLowerCase().replace(/_/g, "-");
    if (normCand === normHint) return true;
    if (normCand.indexOf(normHint) !== -1) return true;
    if (normCand.length >= 3 && normHint.indexOf(normCand) !== -1) return true;
    var dashIdx = normHint.indexOf("-");
    if (dashIdx >= 4) {{
        var stem = normHint.slice(0, dashIdx);
        if (normCand === stem || normCand.indexOf(stem) !== -1) return true;
    }}
    if (normHint.indexOf(".") !== -1) {{
        var parts = normHint.split(".");
        for (var p = parts.length - 1; p >= 0; p--) {{
            var seg = parts[p];
            if (seg.length < 4 || seg === "desktop" || seg === "client" || seg === "linux" || seg === "github") continue;
            if (normCand === seg || normCand.indexOf(seg) !== -1) return true;
            var segDash = seg.indexOf("-");
            if (segDash >= 4) {{
                var segStem = seg.slice(0, segDash);
                if (normCand === segStem || normCand.indexOf(segStem) !== -1) return true;
            }}
        }}
    }}
    return false;
}}

function hintMatches(w) {{
    var hint = cfg.matchHint;
    var desk = cfg.desktopId;
    if (desk && desk.endsWith(".desktop")) desk = desk.slice(0, -8);
    var rc = normalizeText(w.resourceClass);
    var rn = normalizeText(w.resourceName);
    var df = normalizeText(w.desktopFileName);
    if (df.endsWith(".desktop")) df = df.slice(0, -8);
    if (hint && (tokenMatches(rc, hint) || tokenMatches(rn, hint) || tokenMatches(df, hint))) return true;
    if (desk && (tokenMatches(df, desk) || tokenMatches(rc, desk) || tokenMatches(rn, desk))) return true;
    return false;
}}

function restoreOrphanDropdown(w) {{
    var orig = cfg.savedOriginal || {{}};
    w.noBorder = Boolean(orig.noBorder);
    w.keepBelow = Boolean(orig.keepBelow);
    w.keepAbove = Boolean(orig.keepAbove);
    w.skipTaskbar = Boolean(orig.skipTaskbar);
    w.skipPager = Boolean(orig.skipPager);
    w.skipSwitcher = Boolean(orig.skipSwitcher);
    w.onAllDesktops = Boolean(orig.onAllDesktops);
    if (orig.width > 50 && orig.height > 50) {{
        w.frameGeometry = {{x: orig.x, y: orig.y, width: orig.width, height: orig.height}};
    }}
    if (orig.maximizeMode && orig.maximizeMode !== 0 && w.setMaximize) {{
        w.setMaximize((orig.maximizeMode & 1) !== 0, (orig.maximizeMode & 2) !== 0);
    }}
    if (orig.fullScreen) {{
        w.fullScreen = true;
    }}
    w.opacity = (typeof orig.opacity === "number" && orig.opacity > 0.0) ? orig.opacity : 1.0;
    if (w.closeWindow) {{
        w.closeWindow();
    }}
}}

function getTargetScreen() {{
    var screen = null;
    if (cfg.monitorPolicy === "cursor") {{
        screen = workspace.screenAt ? workspace.screenAt(workspace.cursorPos) : workspace.activeScreen;
    }} else if (cfg.monitorPolicy === "primary") {{
        var screens = workspace.screens;
        if (screens && screens.length > 0) screen = screens[0];
    }} else if (cfg.monitorPolicy === "focused") {{
        if (workspace.activeWindow && workspace.activeWindow.output) {{
            screen = workspace.activeWindow.output;
        }} else {{
            screen = workspace.activeScreen;
        }}
    }}
    if (!screen) {{
        screen = workspace.activeScreen;
    }}
    return screen;
}}

function isWindowOnScreen(w, screen) {{
    if (!w || !w.frameGeometry || !screen || !screen.geometry) return true;
    var fg = w.frameGeometry;
    var sg = screen.geometry;
    var cx = fg.x + fg.width / 2.0;
    var cy = fg.y + fg.height / 2.0;
    return cx >= sg.x && cx < (sg.x + sg.width) && cy >= sg.y && cy < (sg.y + sg.height);
}}

var targetScreen = getTargetScreen();
var wins = workspace.windowList ? workspace.windowList() : (workspace.stackingOrder || []);
var foundNormal = false;
for (var i = wins.length - 1; i >= 0; i--) {{
    var w = wins[i];
    if (!w || w.desktopWindow || w.dock || w.splash || w.tooltip || w.notification || w.popupWindow) continue;
    if (w.utility || w.toolbar || w.menubar || w.onScreenDisplay || w.criticalNotification || w.appletPopup) continue;
    if (!hintMatches(w)) continue;
    var isDropdown = !w.minimized && (
        (w.noBorder && (!cfg.keepAbove || w.keepAbove))
        || (cfg.hasActiveWatcher !== false && typeof w.opacity === "number" && w.opacity === 0.0)
    );
    if (isDropdown) {{
        if (flagName) {{
            var shouldRelocate = cfg.relocateOnToggle !== false
                && (cfg.monitorPolicy === "cursor" || cfg.monitorPolicy === "focused")
                && (!isWindowOnScreen(w, targetScreen) || !w.active);
            if (shouldRelocate) {{
                foundNormal = true;
            }} else {{
                if (cfg.hasActiveWatcher === false) {{
                    restoreOrphanDropdown(w);
                }} else {{
                    w.keepBelow = true;
                }}
                callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", flagName);
                foundNormal = false;
            }}
        }} else {{
            foundNormal = true;
        }}
        break;
    }}
    if (!w.minimized) {{
        foundNormal = true;
    }}
}}
if (foundNormal && normalFlagName) {{
    callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", normalFlagName);
}}
if (doneFlagName) {{
    callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", doneFlagName);
}}"""


def render_existing_window_probe_js(
    config: dict[str, Any],
    found_flag_name: str,
    done_flag_name: str = "",
) -> str:
    """Render a KWin 6 JS probe to locate a single-instance window by window_id, match_hint, or desktop_id."""
    cfg_json = json.dumps(config, ensure_ascii=False)
    found_json = json.dumps(found_flag_name, ensure_ascii=False)
    done_json = json.dumps(done_flag_name, ensure_ascii=False)
    return f"""var cfg = {cfg_json};
var foundFlagName = {found_json};
var doneFlagName = {done_json};

function normalizeText(v) {{
    return v ? String(v).toLowerCase() : "";
}}

function tokenMatches(candidate, hint) {{
    if (!candidate || !hint) return false;
    var normCand = String(candidate).toLowerCase().replace(/_/g, "-");
    var normHint = String(hint).toLowerCase().replace(/_/g, "-");
    if (normCand === normHint) return true;
    if (normCand.indexOf(normHint) !== -1) return true;
    if (normCand.length >= 3 && normHint.indexOf(normCand) !== -1) return true;
    var dashIdx = normHint.indexOf("-");
    if (dashIdx >= 4) {{
        var stem = normHint.slice(0, dashIdx);
        if (normCand === stem || normCand.indexOf(stem) !== -1) return true;
    }}
    if (normHint.indexOf(".") !== -1) {{
        var parts = normHint.split(".");
        for (var p = parts.length - 1; p >= 0; p--) {{
            var seg = parts[p];
            if (seg.length < 4 || seg === "desktop" || seg === "client" || seg === "linux" || seg === "github" || seg === "com" || seg === "org" || seg === "net" || seg === "io" || seg === "app") continue;
            if (normCand === seg || normCand.indexOf(seg) !== -1) return true;
            var segDash = seg.indexOf("-");
            if (segDash >= 4) {{
                var segStem = seg.slice(0, segDash);
                if (normCand === segStem || normCand.indexOf(segStem) !== -1) return true;
            }}
        }}
    }}
    return false;
}}

function hintMatches(w) {{
    var hint = cfg.matchHint;
    var desk = cfg.desktopId;
    if (desk && desk.endsWith(".desktop")) desk = desk.slice(0, -8);
    var rc = normalizeText(w.resourceClass);
    var rn = normalizeText(w.resourceName);
    var df = normalizeText(w.desktopFileName);
    if (df.endsWith(".desktop")) df = df.slice(0, -8);
    if (hint && (tokenMatches(rc, hint) || tokenMatches(rn, hint) || tokenMatches(df, hint))) return true;
    if (desk && (tokenMatches(df, desk) || tokenMatches(rc, desk) || tokenMatches(rn, desk))) return true;
    return false;
}}

var wins = workspace.windowList ? workspace.windowList() : (workspace.stackingOrder || []);
var matched = false;
for (var i = wins.length - 1; i >= 0; i--) {{
    var w = wins[i];
    if (!w || w.desktopWindow || w.dock || w.splash || w.tooltip || w.notification || w.popupWindow) continue;
    if (cfg.expectedWindowId && String(w.internalId) === cfg.expectedWindowId) {{
        matched = true;
        break;
    }}
    if (w.noBorder && (!cfg.keepAbove || w.keepAbove) && hintMatches(w)) {{
        matched = true;
        break;
    }}
    if (cfg.allowHintFallback && hintMatches(w)) {{
        matched = true;
        break;
    }}
}}
if (matched && foundFlagName) {{
    callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", foundFlagName);
}}
if (doneFlagName) {{
    callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", doneFlagName);
}}"""
