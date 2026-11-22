/*
    SPDX-License-Identifier: GPL-2.0-or-later
*/

#pragma once

#include "effect/effect.h"

#include <QFileSystemWatcher>
#include <QHash>
#include <QSet>
#include <QString>

namespace KWin
{

class SpawnKeyCornersEffect : public Effect
{
    Q_OBJECT

public:
    explicit SpawnKeyCornersEffect();
    ~SpawnKeyCornersEffect() override;

    static bool supported();

    void reconfigure(ReconfigureFlags flags) override;
    bool isActive() const override;
    QString debug(const QString &parameter) const override;

private Q_SLOTS:
    void onWindowAdded(EffectWindow *w);
    void onWindowClosed(EffectWindow *w);
    void onWindowDeleted(EffectWindow *w);
    void onRuntimeFileChanged();

private:
    struct ProfileRule
    {
        QString profileId;
        QString mode;
        int cornerRadius = 0;
        QSet<pid_t> pids;
        QString matchHint;
        QString desktopId;
        QString windowId;
    };

    QString runtimeDirPath() const;
    QString runtimeFilePath() const;
    void ensureWatcherPaths();
    void reloadRules();
    void syncAllWindows();
    void attachWindowSignals(EffectWindow *w);
    void updateWindow(EffectWindow *w);
    int resolveWindowRadius(EffectWindow *w, QString *matchedProfileId = nullptr) const;
    static bool tokenMatches(const QString &candidate, const QString &hint);
    static bool ruleMatchesWindow(const ProfileRule &rule, EffectWindow *w);

    QFileSystemWatcher m_watcher;
    QHash<QString, ProfileRule> m_rules;
    QHash<EffectWindow *, int> m_activeWindows;
    QHash<EffectWindow *, QString> m_windowProfiles;
    QSet<EffectWindow *> m_observedWindows;
    bool m_updatingRadius = false;
};

} // namespace KWin
