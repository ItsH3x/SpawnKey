import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DBUS_NAME = 'io.github.spawnkey.GnomeShell';
const DBUS_PATH = '/io/github/spawnkey/GnomeShell';
const PROTOCOL_VERSION = '1.0';

const DBUS_INTERFACE_XML = `
<node>
  <interface name="io.github.spawnkey.GnomeShell">
    <method name="Ping">
      <arg type="s" direction="out" name="version"/>
    </method>
    <method name="ToggleDropdown">
      <arg type="s" direction="in" name="config_json"/>
      <arg type="s" direction="out" name="status_json"/>
    </method>
  </interface>
</node>`;

const IGNORED_RDNS_SEGMENTS = new Set([
    'org', 'com', 'net', 'io', 'app', 'desktop', 'client', 'linux', 'github',
]);

function normalizeToken(value) {
    return String(value || '').trim().toLowerCase().replace(/_/g, '-');
}

function tokenMatches(candidate, hint) {
    const normCand = normalizeToken(candidate);
    const normHint = normalizeToken(hint);
    if (!normCand || !normHint)
        return false;

    if (normCand === normHint || normCand.includes(normHint))
        return true;
    if (normCand.length >= 3 && normHint.includes(normCand))
        return true;

    const dashIdx = normHint.indexOf('-');
    if (dashIdx >= 4) {
        const stem = normHint.slice(0, dashIdx);
        if (normCand === stem || normCand.includes(stem))
            return true;
    }

    if (normHint.includes('.')) {
        const parts = normHint.split('.');
        for (let i = parts.length - 1; i >= 0; i--) {
            const seg = parts[i];
            if (seg.length < 4 || IGNORED_RDNS_SEGMENTS.has(seg))
                continue;
            if (normCand === seg || normCand.includes(seg))
                return true;
            const segDash = seg.indexOf('-');
            if (segDash >= 4) {
                const segStem = seg.slice(0, segDash);
                if (normCand === segStem || normCand.includes(segStem))
                    return true;
            }
        }
    }

    return false;
}

function clampDim(val, limit) {
    const maxAllowed = Math.max(1, Math.floor(limit) - 1);
    const minAllowed = Math.min(50, maxAllowed);
    return Math.max(minAllowed, Math.min(Math.round(val), maxAllowed));
}

function computeTargetRect(cfg, area) {
    const rawW = cfg.sizeUnit === 'percent'
        ? area.width * ((Number(cfg.width) || 80) / 100.0)
        : (Number(cfg.width) || 800);
    const rawH = cfg.sizeUnit === 'percent'
        ? area.height * ((Number(cfg.height) || 45) / 100.0)
        : (Number(cfg.height) || 450);

    const width = clampDim(rawW, area.width);
    const height = clampDim(rawH, area.height);

    let relX = 0;
    let relY = 0;
    if ((cfg.positionMode || 'preset') === 'preset') {
        const margin = Math.max(0, Math.round(Number(cfg.edgeMargin) || 0));
        if (cfg.anchorH === 'left')
            relX = margin;
        else if (cfg.anchorH === 'center')
            relX = Math.floor((area.width - width) / 2);
        else
            relX = Math.floor(area.width - width - margin);

        if (cfg.anchorV === 'top')
            relY = margin;
        else if (cfg.anchorV === 'center')
            relY = Math.floor((area.height - height) / 2);
        else
            relY = Math.floor(area.height - height - margin);
    } else {
        const customUnit = cfg.customPositionUnit || cfg.sizeUnit || 'percent';
        relX = customUnit === 'percent'
            ? Math.round(area.width * ((Number(cfg.customX) || 0) / 100.0))
            : Math.round(Number(cfg.customX) || 0);
        relY = customUnit === 'percent'
            ? Math.round(area.height * ((Number(cfg.customY) || 0) / 100.0))
            : Math.round(Number(cfg.customY) || 0);
    }

    const maxRelX = Math.max(0, Math.floor(area.width - width));
    const maxRelY = Math.max(0, Math.floor(area.height - height));
    relX = Math.max(0, Math.min(relX, maxRelX));
    relY = Math.max(0, Math.min(relY, maxRelY));

    return {
        x: Math.round(area.x + relX),
        y: Math.round(area.y + relY),
        width,
        height,
    };
}

export default class SpawnKeyExtension extends Extension {
    enable() {
        this._dbusImpl = null;
        this._busOwnerId = 0;
        this._watchers = new Map();
        this._focusHandlers = new Map();
        this._trackedWindows = new Map();
        this._savedOriginals = new Map();
        this._externalTrayWatchers = new Map();

        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(DBUS_INTERFACE_XML, this);
        this._busOwnerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            DBUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            connection => {
                if (this._dbusImpl)
                    this._dbusImpl.export(connection, DBUS_PATH);
            },
            null,
            null
        );
    }

    disable() {
        for (const profileId of [...this._watchers.keys()])
            this._cancelWatcher(profileId);
        this._watchers.clear();

        for (const profileId of [...this._externalTrayWatchers.keys()])
            this._cancelExternalTrayWatcher(profileId);
        this._externalTrayWatchers.clear();
        this._savedOriginals.clear();

        for (const profileId of [...this._focusHandlers.keys()])
            this._clearFocusHandler(profileId);
        this._focusHandlers.clear();

        for (const entry of this._trackedWindows.values()) {
            if (entry.win && entry.unmanagedId) {
                try {
                    entry.win.disconnect(entry.unmanagedId);
                } catch (_e) {
                    // Window may already be finalized.
                }
            }
        }
        this._trackedWindows.clear();

        if (this._dbusImpl) {
            try {
                this._dbusImpl.unexport();
            } catch (_e) {
                // Ignore if already unexported.
            }
            this._dbusImpl = null;
        }
        if (this._busOwnerId) {
            Gio.bus_unown_name(this._busOwnerId);
            this._busOwnerId = 0;
        }
    }

    Ping() {
        return PROTOCOL_VERSION;
    }

    ToggleDropdown(configJson) {
        let cfg;
        try {
            cfg = JSON.parse(configJson);
        } catch (err) {
            return JSON.stringify({status: 'error', error: `Invalid JSON: ${err.message}`});
        }

        if (!cfg || typeof cfg !== 'object')
            return JSON.stringify({status: 'error', error: 'Config must be a JSON object'});

        const action = String(cfg.action || 'toggle');
        const profileId = String(cfg.profileId || '');

        if (action === 'watch_new') {
            if (profileId)
                this._cancelExternalTrayWatcher(profileId);
            this._armNewWindowWatcher(cfg);
            return JSON.stringify({status: 'watching', profileId});
        }

        if (profileId && action !== 'hide')
            this._cancelExternalTrayWatcher(profileId);

        const win = this._findExistingWindow(cfg);
        if (!win) {
            if (action === 'hide' && profileId) {
                this._cancelWatcher(profileId);
                this._cancelExternalTrayWatcher(profileId);
            }
            return JSON.stringify({status: 'not_found'});
        }

        const windowId = this._windowId(win);
        const pid = typeof win.get_pid === 'function' ? win.get_pid() : -1;
        const mode = String(cfg.mode || 'persistent');

        if (action === 'toggle' && !win.minimized && this._shouldRelocateOrFocus(win, cfg)) {
            this._ensureTrayOriginalCaptured(profileId, win, cfg, mode);
            this._showAndPlaceWindow(win, cfg);
            return JSON.stringify({
                status: 'shown',
                relocated: true,
                windowId,
                pid,
                savedOriginal: (profileId ? this._savedOriginals.get(profileId) : null) || null,
            });
        }

        if (action === 'hide' || (action === 'toggle' && !win.minimized)) {
            this._hideWindow(win, cfg);
            return JSON.stringify({
                status: 'hidden',
                windowId,
                pid,
                savedOriginal: (profileId ? this._savedOriginals.get(profileId) : null) || null,
            });
        }

        this._ensureTrayOriginalCaptured(profileId, win, cfg, mode);
        this._showAndPlaceWindow(win, cfg);
        return JSON.stringify({
            status: 'shown',
            windowId,
            pid,
            savedOriginal: (profileId ? this._savedOriginals.get(profileId) : null) || null,
        });
    }

    _windowId(win) {
        if (win && typeof win.get_stable_sequence === 'function')
            return String(win.get_stable_sequence());
        return '';
    }

    _collectHintTokens(cfg) {
        const tokens = [];
        if (Array.isArray(cfg.matchTokens)) {
            for (const tok of cfg.matchTokens) {
                const s = String(tok || '').trim().toLowerCase();
                if (s && !tokens.includes(s))
                    tokens.push(s);
            }
        }
        const hint = String(cfg.matchHint || '').trim().toLowerCase();
        if (hint && !tokens.includes(hint))
            tokens.push(hint);

        let desk = String(cfg.desktopId || '').trim().toLowerCase();
        if (desk.endsWith('.desktop'))
            desk = desk.slice(0, -8);
        if (desk && !tokens.includes(desk))
            tokens.push(desk);

        return tokens;
    }

    _windowMatchesHints(win, cfg) {
        const tokens = this._collectHintTokens(cfg);
        if (tokens.length === 0)
            return false;

        const candidates = [
            win.get_wm_class?.() ?? '',
            win.get_wm_class_instance?.() ?? '',
            win.get_gtk_application_id?.() ?? '',
            win.get_sandboxed_app_id?.() ?? '',
        ];

        for (const cand of candidates) {
            if (!cand)
                continue;
            for (const tok of tokens) {
                if (tokenMatches(cand, tok))
                    return true;
            }
        }
        return false;
    }

    _isNormalAppWindow(win) {
        if (!win)
            return false;
        if (typeof win.get_window_type === 'function')
            return win.get_window_type() === Meta.WindowType.NORMAL;
        return true;
    }

    _isTrackedWindowForProfile(win, profileId) {
        if (!profileId)
            return false;
        const entry = this._trackedWindows.get(profileId);
        return Boolean(entry && entry.win === win);
    }

    _findExistingWindow(cfg) {
        const actors = global.get_window_actors ? global.get_window_actors() : [];
        const expectedId = String(cfg.windowId || '');
        const pids = Array.isArray(cfg.pids) ? cfg.pids : [];
        const profileId = String(cfg.profileId || '');

        if (expectedId) {
            for (let i = actors.length - 1; i >= 0; i--) {
                const win = actors[i]?.get_meta_window?.();
                if (this._isNormalAppWindow(win) && this._windowId(win) === expectedId)
                    return win;
            }
        }

        if (pids.length > 0) {
            for (let i = actors.length - 1; i >= 0; i--) {
                const win = actors[i]?.get_meta_window?.();
                if (!this._isNormalAppWindow(win))
                    continue;
                const pid = win.get_pid?.() ?? -1;
                if (pids.includes(pid))
                    return win;
            }
        }

        const trackedEntry = profileId ? this._trackedWindows.get(profileId) : null;
        if (trackedEntry && trackedEntry.win && this._isNormalAppWindow(trackedEntry.win)) {
            for (let i = actors.length - 1; i >= 0; i--) {
                if (actors[i]?.get_meta_window?.() === trackedEntry.win)
                    return trackedEntry.win;
            }
        }

        for (let i = actors.length - 1; i >= 0; i--) {
            const win = actors[i]?.get_meta_window?.();
            if (!this._isNormalAppWindow(win))
                continue;
            if (win.is_skip_taskbar?.() && !this._isTrackedWindowForProfile(win, profileId))
                continue;
            if (this._windowMatchesHints(win, cfg))
                return win;
        }

        return null;
    }

    _resolveTargetMonitorIndex(cfg) {
        const nMonitors = global.display.get_n_monitors();
        let monIdx = -1;
        const policy = String(cfg.monitorPolicy || 'cursor');

        if (policy === 'primary') {
            monIdx = global.display.get_primary_monitor();
        } else if (policy === 'focused') {
            const focused = global.display.focus_window;
            monIdx = focused ? focused.get_monitor() : global.display.get_current_monitor();
        } else {
            monIdx = global.display.get_current_monitor();
        }

        if (monIdx < 0 || monIdx >= nMonitors)
            monIdx = global.display.get_primary_monitor();
        if (monIdx < 0 || monIdx >= nMonitors)
            monIdx = 0;

        return monIdx;
    }

    _resolveMonitorWorkArea(cfg) {
        const monIdx = this._resolveTargetMonitorIndex(cfg);
        const workspace = global.workspace_manager.get_active_workspace();
        let area = workspace?.get_work_area_for_monitor?.(monIdx);
        if (!area || area.width <= 1 || area.height <= 1)
            area = global.display.get_monitor_geometry(monIdx);

        return area;
    }

    _shouldRelocateOrFocus(win, cfg) {
        if (!win || cfg.relocateOnToggle === false)
            return false;
        const policy = String(cfg.monitorPolicy || 'cursor');
        if (policy !== 'cursor' && policy !== 'focused')
            return false;
        const targetMonIdx = this._resolveTargetMonitorIndex(cfg);
        if (typeof win.get_monitor === 'function' && win.get_monitor() !== targetMonIdx)
            return true;
        const focused = global.display.focus_window;
        if (focused !== win && focused?.get_transient_for?.() !== win)
            return true;
        return false;
    }

    _captureOriginalState(win, fallbackOpacity = 255) {
        if (!win)
            return null;
        const rect = typeof win.get_frame_rect === 'function' ? win.get_frame_rect() : null;
        const maximizedFlags = typeof win.get_maximized === 'function'
            ? win.get_maximized()
            : (win.is_maximized?.() ? (Meta.MaximizeFlags?.BOTH ?? 3) : 0);
        const actor = win.get_compositor_private?.();
        const opacity = actor && typeof actor.opacity === 'number' && actor.opacity > 0
            ? actor.opacity
            : fallbackOpacity;
        return {
            x: rect ? Math.round(rect.x) : 0,
            y: rect ? Math.round(rect.y) : 0,
            width: rect ? Math.round(rect.width) : 0,
            height: rect ? Math.round(rect.height) : 0,
            maximizedFlags: Number(maximizedFlags) || 0,
            fullscreen: Boolean(win.fullscreen),
            opacity,
        };
    }

    _ensureTrayOriginalCaptured(profileId, win, cfg, mode) {
        if (mode !== 'tray' || !profileId || this._savedOriginals.has(profileId))
            return;
        if (this._isTrackedWindowForProfile(win, profileId)) {
            if (cfg.savedOriginal && typeof cfg.savedOriginal === 'object')
                this._savedOriginals.set(profileId, cfg.savedOriginal);
            return;
        }
        const captured = this._captureOriginalState(win);
        if (captured)
            this._savedOriginals.set(profileId, captured);
    }

    _unmaximizeWindow(win) {
        const isMaximized = typeof win.is_maximized === 'function'
            ? win.is_maximized()
            : (typeof win.get_maximized === 'function' ? win.get_maximized() !== 0 : false);

        if (isMaximized && typeof win.unmaximize === 'function') {
            try {
                if (win.unmaximize.length === 0 || !Meta.MaximizeFlags)
                    win.unmaximize();
                else
                    win.unmaximize(Meta.MaximizeFlags.BOTH);
            } catch (_e) {
                try {
                    win.unmaximize();
                } catch (_e2) {
                    // Best-effort unmaximize.
                }
            }
        }

        if (win.fullscreen && typeof win.unmake_fullscreen === 'function')
            win.unmake_fullscreen();
    }

    _trackWindow(profileId, win) {
        if (!profileId || !win)
            return;
        const prev = this._trackedWindows.get(profileId);
        if (prev && prev.win === win)
            return;
        if (prev && prev.win && prev.unmanagedId) {
            try {
                prev.win.disconnect(prev.unmanagedId);
            } catch (_e) {
                // Ignore stale window.
            }
        }

        const unmanagedId = win.connect('unmanaged', () => {
            this._clearFocusHandler(profileId);
            const cur = this._trackedWindows.get(profileId);
            if (cur && cur.win === win)
                this._trackedWindows.delete(profileId);
        });
        this._trackedWindows.set(profileId, {win, unmanagedId});
    }

    _showAndPlaceWindow(win, cfg) {
        const profileId = String(cfg.profileId || '');
        this._trackWindow(profileId, win);

        if (win.minimized)
            win.unminimize();

        this._unmaximizeWindow(win);

        const area = this._resolveMonitorWorkArea(cfg);
        const target = computeTargetRect(cfg, area);
        win.move_resize_frame(true, target.x, target.y, target.width, target.height);

        if (cfg.keepAbove)
            win.make_above?.();
        else
            win.unmake_above?.();

        win.stick?.();

        const targetOpacity = Math.max(
            26,
            Math.min(255, Math.round((Number(cfg.opacity ?? 1.0)) * 255))
        );
        const actor = win.get_compositor_private?.();
        if (actor) {
            actor.remove_all_transitions?.();
            const animType = String(cfg.animationType || 'slide-fade');
            const durationMs = Math.max(0, Math.round(Number(cfg.durationMs) || 0));

            if (animType === 'none' || durationMs <= 0 || typeof actor.ease !== 'function') {
                actor.translation_x = 0;
                actor.translation_y = 0;
                actor.opacity = targetOpacity;
            } else {
                const useSlide = animType === 'slide' || animType === 'slide-fade';
                const useFade = animType === 'fade' || animType === 'slide-fade';

                let dx = 0;
                let dy = 0;
                if (useSlide) {
                    const stepX = Math.min(target.width, Math.max(120, Math.round(area.width * 0.35)));
                    const stepY = Math.min(target.height, Math.max(120, Math.round(area.height * 0.35)));
                    const direction = String(cfg.direction || 'top');
                    if (direction === 'top')
                        dy = -stepY;
                    else if (direction === 'bottom')
                        dy = stepY;
                    else if (direction === 'left')
                        dx = -stepX;
                    else if (direction === 'right')
                        dx = stepX;
                }

                actor.translation_x = dx;
                actor.translation_y = dy;
                actor.opacity = useFade ? 0 : targetOpacity;
                actor.ease({
                    translation_x: 0,
                    translation_y: 0,
                    opacity: targetOpacity,
                    duration: durationMs,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            }
        }

        win.activate(global.get_current_time());

        if (cfg.hideOnClickOutside)
            this._armFocusOutsideWatcher(win, cfg);
        else
            this._clearFocusHandler(profileId);
    }

    _hideWindow(win, cfg) {
        const profileId = String(cfg.profileId || '');
        this._clearFocusHandler(profileId);

        const mode = String(cfg.mode || 'persistent');
        if (mode === 'tray') {
            if (profileId) {
                if (!this._savedOriginals.has(profileId) && cfg.savedOriginal && typeof cfg.savedOriginal === 'object')
                    this._savedOriginals.set(profileId, cfg.savedOriginal);
                this._armExternalTrayRestoreWatcher(profileId, cfg);
            }
            win.delete(global.get_current_time());
        } else if (mode === 'ephemeral') {
            if (profileId)
                this._savedOriginals.delete(profileId);
            win.delete(global.get_current_time());
        } else {
            win.minimize();
        }
    }

    _clearFocusHandler(profileId) {
        if (!profileId)
            return;
        const entry = this._focusHandlers.get(profileId);
        if (!entry)
            return;
        this._focusHandlers.delete(profileId);

        if (entry.focusId) {
            try {
                global.display.disconnect(entry.focusId);
            } catch (_e) {
                // Ignore.
            }
        }
        if (entry.minId && entry.win) {
            try {
                entry.win.disconnect(entry.minId);
            } catch (_e) {
                // Ignore.
            }
        }
    }

    _armFocusOutsideWatcher(win, cfg) {
        const profileId = String(cfg.profileId || '');
        if (!profileId)
            return;
        this._clearFocusHandler(profileId);

        const entry = {win, focusId: 0, minId: 0};
        entry.focusId = global.display.connect('notify::focus-window', () => {
            const focused = global.display.focus_window;
            if (focused === win)
                return;
            if (focused && focused.get_transient_for?.() === win)
                return;
            if (win.minimized) {
                this._clearFocusHandler(profileId);
                return;
            }
            this._hideWindow(win, cfg);
        });

        entry.minId = win.connect('notify::minimized', () => {
            if (win.minimized)
                this._clearFocusHandler(profileId);
        });

        this._focusHandlers.set(profileId, entry);
    }

    _cancelWatcher(profileId) {
        const watcher = this._watchers.get(profileId);
        if (!watcher)
            return;
        this._watchers.delete(profileId);

        if (watcher.createdId) {
            try {
                global.display.disconnect(watcher.createdId);
            } catch (_e) {
                // Ignore.
            }
        }
        if (watcher.timeoutId)
            GLib.Source.remove(watcher.timeoutId);
        if (watcher.fallbackId)
            GLib.Source.remove(watcher.fallbackId);
        if (watcher.firstFrameId && watcher.actor) {
            try {
                watcher.actor.disconnect(watcher.firstFrameId);
            } catch (_e) {
                // Ignore.
            }
        }
    }

    _armNewWindowWatcher(cfg) {
        const profileId = String(cfg.profileId || '');
        if (!profileId)
            return;

        this._cancelWatcher(profileId);

        const watcher = {
            createdId: 0,
            timeoutId: 0,
            fallbackId: 0,
            firstFrameId: 0,
            actor: null,
        };
        this._watchers.set(profileId, watcher);

        const pids = Array.isArray(cfg.pids) ? cfg.pids : [];
        const timeoutMs = Math.max(1000, Math.min(60000, Math.round(Number(cfg.startupTimeoutMs) || 8000)));

        watcher.createdId = global.display.connect('window-created', (_display, win) => {
            if (!this._isNormalAppWindow(win))
                return;

            const pid = win.get_pid?.() ?? -1;
            const pidMatch = pids.length > 0 && pids.includes(pid);
            const hintMatch = this._windowMatchesHints(win, cfg);
            if (!pidMatch && !hintMatch)
                return;

            // One-shot match: detach window-created and timeout immediately.
            if (watcher.createdId) {
                global.display.disconnect(watcher.createdId);
                watcher.createdId = 0;
            }
            if (watcher.timeoutId) {
                GLib.Source.remove(watcher.timeoutId);
                watcher.timeoutId = 0;
            }

            const actor = win.get_compositor_private?.();
            const initialOpacity = actor && typeof actor.opacity === 'number' && actor.opacity > 0
                ? actor.opacity
                : 255;
            let placed = false;
            const finishPlacement = () => {
                if (placed)
                    return;
                placed = true;
                this._cancelWatcher(profileId);
                if (String(cfg.mode || '') === 'tray' && !this._savedOriginals.has(profileId)) {
                    const captured = this._captureOriginalState(win, initialOpacity);
                    if (captured && captured.width <= 50 && cfg.savedOriginal && Number(cfg.savedOriginal.width) > 50)
                        this._savedOriginals.set(profileId, cfg.savedOriginal);
                    else if (captured)
                        this._savedOriginals.set(profileId, captured);
                }
                this._showAndPlaceWindow(win, cfg);
            };

            if (actor) {
                actor.opacity = 0;
                watcher.actor = actor;
                watcher.firstFrameId = actor.connect('first-frame', () => {
                    finishPlacement();
                });
                watcher.fallbackId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 25, () => {
                    watcher.fallbackId = 0;
                    finishPlacement();
                    return GLib.SOURCE_REMOVE;
                });
            } else {
                watcher.fallbackId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 25, () => {
                    watcher.fallbackId = 0;
                    finishPlacement();
                    return GLib.SOURCE_REMOVE;
                });
            }
        });

        watcher.timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, timeoutMs, () => {
            watcher.timeoutId = 0;
            this._cancelWatcher(profileId);
            return GLib.SOURCE_REMOVE;
        });
    }

    _cancelExternalTrayWatcher(profileId) {
        if (!profileId)
            return;
        const watcher = this._externalTrayWatchers.get(profileId);
        if (!watcher)
            return;
        this._externalTrayWatchers.delete(profileId);

        if (watcher.createdId) {
            try {
                global.display.disconnect(watcher.createdId);
            } catch (_e) {
                // Ignore.
            }
        }
        if (watcher.fallbackId)
            GLib.Source.remove(watcher.fallbackId);
        if (watcher.firstFrameId && watcher.actor) {
            try {
                watcher.actor.disconnect(watcher.firstFrameId);
            } catch (_e) {
                // Ignore.
            }
        }
    }

    _restoreWindowToOriginal(win, savedOrig) {
        if (!win)
            return;
        try {
            win.unmake_above?.();
        } catch (_e) {
            // Ignore.
        }
        try {
            win.unstick?.();
        } catch (_e) {
            // Ignore.
        }
        if (savedOrig && typeof savedOrig === 'object') {
            if (savedOrig.fullscreen) {
                if (typeof win.make_fullscreen === 'function')
                    win.make_fullscreen();
            } else {
                if (win.fullscreen && typeof win.unmake_fullscreen === 'function')
                    win.unmake_fullscreen();
                const maxFlags = Number(savedOrig.maximizedFlags) || 0;
                if (maxFlags !== 0 && typeof win.maximize === 'function') {
                    try {
                        if (win.maximize.length === 0 || !Meta.MaximizeFlags)
                            win.maximize();
                        else
                            win.maximize(maxFlags);
                    } catch (_e) {
                        try {
                            win.maximize();
                        } catch (_e2) {
                            // Best-effort maximize.
                        }
                    }
                } else {
                    this._unmaximizeWindow(win);
                    if (Number(savedOrig.width) > 50 && Number(savedOrig.height) > 50) {
                        win.move_resize_frame(
                            true,
                            Math.round(Number(savedOrig.x) || 0),
                            Math.round(Number(savedOrig.y) || 0),
                            Math.round(Number(savedOrig.width)),
                            Math.round(Number(savedOrig.height))
                        );
                    }
                }
            }
        }
        const actor = win.get_compositor_private?.();
        if (actor) {
            const origOp = savedOrig && typeof savedOrig.opacity === 'number' && savedOrig.opacity > 0
                ? savedOrig.opacity
                : 255;
            actor.opacity = Math.max(26, Math.min(255, Math.round(origOp)));
        }
        win.activate?.(global.get_current_time());
    }

    _armExternalTrayRestoreWatcher(profileId, cfg) {
        if (!profileId)
            return;
        this._cancelExternalTrayWatcher(profileId);

        const watcher = {
            createdId: 0,
            fallbackId: 0,
            firstFrameId: 0,
            actor: null,
        };
        this._externalTrayWatchers.set(profileId, watcher);

        const pids = Array.isArray(cfg.pids) ? cfg.pids : [];
        watcher.createdId = global.display.connect('window-created', (_display, win) => {
            if (!this._isNormalAppWindow(win))
                return;

            const pid = win.get_pid?.() ?? -1;
            const pidMatch = pids.length > 0 && pids.includes(pid);
            const hintMatch = this._windowMatchesHints(win, cfg);
            if (!pidMatch && !hintMatch)
                return;

            if (watcher.createdId) {
                try {
                    global.display.disconnect(watcher.createdId);
                } catch (_e) {
                    // Ignore.
                }
                watcher.createdId = 0;
            }

            const savedOrig = this._savedOriginals.get(profileId) || cfg.savedOriginal || null;
            const actor = win.get_compositor_private?.();
            let restored = false;
            const finishRestore = () => {
                if (restored)
                    return;
                restored = true;
                this._cancelExternalTrayWatcher(profileId);
                this._savedOriginals.delete(profileId);
                this._restoreWindowToOriginal(win, savedOrig);
            };

            if (actor) {
                actor.opacity = 0;
                watcher.actor = actor;
                watcher.firstFrameId = actor.connect('first-frame', () => {
                    finishRestore();
                });
                watcher.fallbackId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 40, () => {
                    watcher.fallbackId = 0;
                    finishRestore();
                    return GLib.SOURCE_REMOVE;
                });
            } else {
                watcher.fallbackId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 40, () => {
                    watcher.fallbackId = 0;
                    finishRestore();
                    return GLib.SOURCE_REMOVE;
                });
            }
        });
    }
}
