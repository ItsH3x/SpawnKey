/*
    SPDX-License-Identifier: GPL-2.0-or-later
*/

#include "spawnkey_corners.h"

#include "effect/effecthandler.h"
#include "effect/effectwindow.h"
#include "scene/borderradius.h"
#include "window.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSet>
#include <QStandardPaths>
#include <QStringList>
#include <algorithm>
#include <unistd.h>

__attribute__((constructor)) static void registerSpawnKeyUserPluginPath()
{
    if (!QCoreApplication::instance()) {
        return;
    }
    const QString userPlugins = QDir::homePath() + QStringLiteral("/.local/lib/qt6/plugins");
    if (!QCoreApplication::libraryPaths().contains(userPlugins)) {
        QCoreApplication::addLibraryPath(userPlugins);
    }
}

namespace KWin
{

SpawnKeyCornersEffect::SpawnKeyCornersEffect()
    : Effect()
{
    connect(&m_watcher, &QFileSystemWatcher::fileChanged, this, &SpawnKeyCornersEffect::onRuntimeFileChanged);
    connect(&m_watcher, &QFileSystemWatcher::directoryChanged, this, &SpawnKeyCornersEffect::onRuntimeFileChanged);

    if (effects) {
        connect(effects, &EffectsHandler::windowAdded, this, &SpawnKeyCornersEffect::onWindowAdded);
        connect(effects, &EffectsHandler::windowClosed, this, &SpawnKeyCornersEffect::onWindowClosed);
        connect(effects, &EffectsHandler::windowDeleted, this, &SpawnKeyCornersEffect::onWindowDeleted);
    }

    ensureWatcherPaths();
    reloadRules();
}

SpawnKeyCornersEffect::~SpawnKeyCornersEffect()
{
    m_updatingRadius = true;
    for (auto it = m_activeWindows.cbegin(); it != m_activeWindows.cend(); ++it) {
        if (EffectWindow *w = it.key()) {
            if (Window *kw = w->window()) {
                kw->setBorderRadius(BorderRadius());
            }
        }
    }
    m_updatingRadius = false;
}

bool SpawnKeyCornersEffect::supported()
{
    return effects != nullptr;
}

bool SpawnKeyCornersEffect::isActive() const
{
    // Clipping is performed natively by KWin's ItemRendererOpenGL via Window::setBorderRadius().
    return false;
}

QString SpawnKeyCornersEffect::runtimeDirPath() const
{
    QString base = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (base.isEmpty()) {
        base = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
    }
    if (base.isEmpty()) {
        base = QStringLiteral("/tmp/spawnkey-%1").arg(::getuid());
    }
    return base + QStringLiteral("/spawnkey");
}

QString SpawnKeyCornersEffect::runtimeFilePath() const
{
    return runtimeDirPath() + QStringLiteral("/corner_radius.json");
}

void SpawnKeyCornersEffect::ensureWatcherPaths()
{
    const QString dirPath = runtimeDirPath();
    if (QDir(dirPath).exists() && !m_watcher.directories().contains(dirPath)) {
        m_watcher.addPath(dirPath);
    }
    const QString filePath = runtimeFilePath();
    if (QFileInfo::exists(filePath) && !m_watcher.files().contains(filePath)) {
        m_watcher.addPath(filePath);
    }
}

void SpawnKeyCornersEffect::reconfigure(ReconfigureFlags /*flags*/)
{
    ensureWatcherPaths();
    reloadRules();
}

void SpawnKeyCornersEffect::onRuntimeFileChanged()
{
    ensureWatcherPaths();
    reloadRules();
}

void SpawnKeyCornersEffect::reloadRules()
{
    QHash<QString, ProfileRule> nextRules;
    QFile file(runtimeFilePath());
    if (file.exists() && file.open(QIODevice::ReadOnly)) {
        const QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
        file.close();
        if (doc.isObject()) {
            const QJsonObject profilesObj = doc.object().value(QStringLiteral("profiles")).toObject();
            for (auto it = profilesObj.begin(); it != profilesObj.end(); ++it) {
                if (!it.value().isObject()) {
                    continue;
                }
                const QJsonObject item = it.value().toObject();
                const int radius = std::clamp(item.value(QStringLiteral("corner_radius")).toInt(0), 0, 128);
                if (radius <= 0) {
                    continue;
                }
                ProfileRule rule;
                rule.profileId = it.key();
                rule.mode = item.value(QStringLiteral("mode")).toString(QStringLiteral("persistent"));
                rule.cornerRadius = radius;
                rule.matchHint = item.value(QStringLiteral("match_hint")).toString().trimmed().toLower();
                rule.desktopId = item.value(QStringLiteral("desktop_id")).toString().trimmed().toLower();
                if (rule.desktopId.endsWith(QStringLiteral(".desktop"))) {
                    rule.desktopId.chop(8);
                }
                rule.windowId = item.value(QStringLiteral("window_id")).toString().trimmed();

                const QJsonArray pidsArr = item.value(QStringLiteral("pids")).toArray();
                for (const QJsonValue &v : pidsArr) {
                    const int pid = v.toInt(0);
                    if (pid > 0) {
                        rule.pids.insert(static_cast<pid_t>(pid));
                    }
                }
                nextRules.insert(rule.profileId, rule);
            }
        }
    }

    m_rules = std::move(nextRules);
    syncAllWindows();
}

bool SpawnKeyCornersEffect::tokenMatches(const QString &candidate, const QString &hint)
{
    if (candidate.isEmpty() || hint.isEmpty()) {
        return false;
    }
    QString normCand = candidate.trimmed().toLower();
    normCand.replace(QLatin1Char('_'), QLatin1Char('-'));
    QString normHint = hint.trimmed().toLower();
    normHint.replace(QLatin1Char('_'), QLatin1Char('-'));
    if (normCand.isEmpty() || normHint.isEmpty()) {
        return false;
    }
    if (normCand == normHint || normCand.contains(normHint)) {
        return true;
    }
    if (normCand.size() >= 3 && normHint.contains(normCand)) {
        return true;
    }
    const qsizetype dashIdx = normHint.indexOf(QLatin1Char('-'));
    if (dashIdx >= 4) {
        const QString stem = normHint.left(dashIdx);
        if (normCand == stem || normCand.contains(stem)) {
            return true;
        }
    }
    if (normHint.contains(QLatin1Char('.'))) {
        static const QSet<QString> ignored{
            QStringLiteral("desktop"),
            QStringLiteral("client"),
            QStringLiteral("linux"),
            QStringLiteral("github"),
            QStringLiteral("com"),
            QStringLiteral("org"),
            QStringLiteral("net"),
            QStringLiteral("io"),
            QStringLiteral("app"),
        };
        const QStringList parts = normHint.split(QLatin1Char('.'), Qt::SkipEmptyParts);
        for (qsizetype p = parts.size() - 1; p >= 0; --p) {
            const QString &seg = parts.at(p);
            if (seg.size() < 4 || ignored.contains(seg)) {
                continue;
            }
            if (normCand == seg || normCand.contains(seg)) {
                return true;
            }
            const qsizetype segDash = seg.indexOf(QLatin1Char('-'));
            if (segDash >= 4) {
                const QString segStem = seg.left(segDash);
                if (normCand == segStem || normCand.contains(segStem)) {
                    return true;
                }
            }
        }
    }
    return false;
}

bool SpawnKeyCornersEffect::ruleMatchesWindow(const ProfileRule &rule, EffectWindow *w)
{
    if (!w) {
        return false;
    }
    if (!rule.windowId.isEmpty() && w->internalId().toString() == rule.windowId) {
        return true;
    }
    const pid_t winPid = w->pid();
    if (winPid > 0 && rule.pids.contains(winPid)) {
        return true;
    }

    Window *kw = w->window();
    const QString rc = kw ? kw->resourceClass().trimmed().toLower() : QString();
    const QString rn = kw ? kw->resourceName().trimmed().toLower() : QString();
    QString df = kw ? kw->desktopFileName().trimmed().toLower() : QString();
    if (df.endsWith(QStringLiteral(".desktop"))) {
        df.chop(8);
    }
    const QString wc = w->windowClass().trimmed().toLower();

    if (!rule.matchHint.isEmpty()) {
        if (tokenMatches(rc, rule.matchHint)
            || tokenMatches(rn, rule.matchHint)
            || tokenMatches(df, rule.matchHint)
            || tokenMatches(wc, rule.matchHint)) {
            return true;
        }
    }
    if (!rule.desktopId.isEmpty()) {
        if (tokenMatches(df, rule.desktopId)
            || tokenMatches(rc, rule.desktopId)
            || tokenMatches(rn, rule.desktopId)
            || tokenMatches(wc, rule.desktopId)) {
            return true;
        }
    }
    return false;
}

int SpawnKeyCornersEffect::resolveWindowRadius(EffectWindow *w, QString *matchedProfileId) const
{
    if (!w || w->isDeleted() || m_rules.isEmpty()) {
        return 0;
    }
    if (w->isDesktop() || w->isDock() || w->isSplash() || w->isTooltip()
        || w->isNotification() || w->isCriticalNotification()
        || w->isOnScreenDisplay() || w->isPopupWindow()
        || w->isOutline() || w->isLockScreen() || w->isAppletPopup()) {
        return 0;
    }

    Window *kw = w->window();
    // Active SpawnKey dropdowns always enforce borderless + onAllDesktops
    if (!kw || !kw->noBorder() || !kw->isOnAllDesktops()) {
        return 0;
    }

    for (auto it = m_rules.cbegin(); it != m_rules.cend(); ++it) {
        if (ruleMatchesWindow(it.value(), w)) {
            if (matchedProfileId) {
                *matchedProfileId = it.key();
            }
            return it.value().cornerRadius;
        }
    }
    return 0;
}

void SpawnKeyCornersEffect::attachWindowSignals(EffectWindow *w)
{
    if (!w || m_observedWindows.contains(w)) {
        return;
    }
    Window *kw = w->window();
    if (!kw) {
        return;
    }
    m_observedWindows.insert(w);

    connect(kw, &Window::decorationPolicyChanged, this, [this, w]() {
        updateWindow(w);
    });
    connect(kw, &Window::desktopsChanged, this, [this, w]() {
        updateWindow(w);
    });
    connect(kw, &Window::borderRadiusChanged, this, [this, w]() {
        if (!m_updatingRadius && m_activeWindows.contains(w)) {
            updateWindow(w);
        }
    });
}

void SpawnKeyCornersEffect::updateWindow(EffectWindow *w)
{
    if (!w) {
        return;
    }
    Window *kw = w->window();
    if (!kw) {
        return;
    }

    QString matchedId;
    const int radius = resolveWindowRadius(w, &matchedId);
    const int prevRadius = m_activeWindows.value(w, 0);

    if (radius > 0) {
        m_activeWindows.insert(w, radius);
        m_windowProfiles.insert(w, matchedId);
        const BorderRadius target(static_cast<qreal>(radius));
        if (kw->borderRadius() != target) {
            m_updatingRadius = true;
            kw->setBorderRadius(target);
            m_updatingRadius = false;
            w->addRepaintFull();
        }
    } else if (prevRadius > 0) {
        m_activeWindows.remove(w);
        m_windowProfiles.remove(w);
        m_updatingRadius = true;
        kw->setBorderRadius(BorderRadius());
        m_updatingRadius = false;
        w->addRepaintFull();
    }
}

void SpawnKeyCornersEffect::syncAllWindows()
{
    if (!effects) {
        return;
    }
    const QList<EffectWindow *> wins = effects->stackingOrder();
    for (EffectWindow *w : wins) {
        attachWindowSignals(w);
        updateWindow(w);
    }
}

void SpawnKeyCornersEffect::onWindowAdded(EffectWindow *w)
{
    attachWindowSignals(w);
    updateWindow(w);
}

void SpawnKeyCornersEffect::onWindowClosed(EffectWindow *w)
{
    const QString profileId = m_windowProfiles.take(w);
    if (m_activeWindows.remove(w) > 0) {
        if (Window *kw = w ? w->window() : nullptr) {
            m_updatingRadius = true;
            kw->setBorderRadius(BorderRadius());
            m_updatingRadius = false;
        }
    }
    if (!profileId.isEmpty()) {
        const auto it = m_rules.constFind(profileId);
        if (it != m_rules.cend() && (it->mode == QStringLiteral("tray") || it->mode == QStringLiteral("ephemeral"))) {
            m_rules.erase(it);
        }
    }
}

void SpawnKeyCornersEffect::onWindowDeleted(EffectWindow *w)
{
    m_observedWindows.remove(w);
    m_windowProfiles.remove(w);
    m_activeWindows.remove(w);
}

QString SpawnKeyCornersEffect::debug(const QString &parameter) const
{
    if (parameter == QStringLiteral("reload")) {
        auto *self = const_cast<SpawnKeyCornersEffect *>(this);
        self->ensureWatcherPaths();
        self->reloadRules();
    }
    return QStringLiteral("rules=%1 active=%2").arg(m_rules.size()).arg(m_activeWindows.size());
}

KWIN_EFFECT_FACTORY_SUPPORTED(SpawnKeyCornersEffect,
                              "metadata.json",
                              return SpawnKeyCornersEffect::supported();)

} // namespace KWin

#include "spawnkey_corners.moc"
