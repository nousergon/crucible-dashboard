#!/bin/bash
# install-box-health.sh — One-time installer for the dashboard EC2 watchdog.
#
# Copies box_health.sh -> /usr/local/bin (a non-repo path, so the box's git
# working tree never collides with the live script), installs the
# box-health.service + .timer units, and enables the timer (every 10 min).
# Must run as root via sudo. Idempotent — re-run to apply updated files.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-box-health.sh

set -euo pipefail

REPO_INFRA="/home/ec2-user/alpha-engine-dashboard/infrastructure"
SCRIPT_SRC="$REPO_INFRA/box_health.sh"
SCRIPT_DST="/usr/local/bin/box_health.sh"
SYSTEMD_SRC="$REPO_INFRA/systemd"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi
if [ ! -f "$SCRIPT_SRC" ]; then
    echo "ERROR: $SCRIPT_SRC not found — pull alpha-engine-dashboard first" >&2
    exit 1
fi

install -m 0755 "$SCRIPT_SRC" "$SCRIPT_DST"
echo "Installed $SCRIPT_DST"

install -m 0755 "$REPO_INFRA/box_hygiene.sh" /usr/local/bin/box_hygiene.sh
echo "Installed /usr/local/bin/box_hygiene.sh"

# The ALERT_PY helper travels WITH the publisher (alpha-engine-config-I7168).
#
# box_health.sh sources `$(dirname "$BASH_SOURCE")/alert_py.sh` first and a
# hardcoded checkout path second. Without this line the sibling never exists,
# so every installed publisher on this box depends on the literal
# `/home/ec2-user/alpha-engine-dashboard/infrastructure/alert_py.sh` — a
# LEGACY directory name (this repo is crucible-dashboard now) that nothing
# tests, nothing owns, and one `mv` would take out from under all six
# publishers at once.
#
# It is also what made 2026-08-13 a CRITICAL: box_health.sh was installed
# carrying `. /usr/local/bin/alert_py.sh` before any fallback existed, the file
# was not there, and the box's primary watchdog exited 1 on
# `ALERT_PY: unbound variable`. The fallback added afterwards keeps that from
# being fatal; installing the helper is what stops it being load-bearing.
install -m 0755 "$REPO_INFRA/alert_py.sh" /usr/local/bin/alert_py.sh
echo "Installed /usr/local/bin/alert_py.sh"

# The hygiene emitter travels WITH the publisher, for the same reason and by
# the same rule (alpha-engine-config-I7168). box_health.sh resolves it as
# `$(dirname "$BASH_SOURCE")/emit_box_health_hygiene.py`, so an installed
# box_health.sh with no sibling here silently loses the ENTIRE info tier: the
# helper's guard returns 0, the run stays green, and the console row goes
# stale — which is exactly the shape of failure the routing change was made to
# avoid, arriving through the installer instead of the classifier.
install -m 0755 "$REPO_INFRA/emit_box_health_hygiene.py" /usr/local/bin/emit_box_health_hygiene.py
echo "Installed /usr/local/bin/emit_box_health_hygiene.py"

# git-sync-lock.sh travels WITH box_health.sh for the same reason as
# alert_py.sh and the hygiene emitter above (alpha-engine-config-I8990):
# check_morning_signal_sync_drift resolves git_sync_state_path() as
# `$(dirname "$BASH_SOURCE")/lib/git-sync-lock.sh` (in-repo) or
# `$(dirname "$BASH_SOURCE")/git-sync-lock.sh` (installed, flat) — the same
# two-candidate pattern install-morning-signal.sh already uses for
# morning-signal-sync.sh. Installing it here too is idempotent: it is the
# identical file morning-signal's own installer places at
# /usr/local/bin/git-sync-lock.sh, just written by whichever installer runs
# second.
install -m 0644 "$REPO_INFRA/lib/git-sync-lock.sh" /usr/local/bin/git-sync-lock.sh
echo "Installed /usr/local/bin/git-sync-lock.sh"

# box-state-backup runs the script from the REPO path (like substrate-health
# and daily-news) rather than a /usr/local/bin copy: it reads budget.yaml from
# a path relative to itself, so splitting the two would need a second copy of
# the registry — which is the drift this registry exists to end.
for unit in box-health.service box-health.timer box-hygiene.service box-hygiene.timer \
            box-state-backup.service box-state-backup.timer; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

# journald size cap (config#2227) — restart journald only when the drop-in
# actually changed, so routine re-runs don't bounce the journal.
mkdir -p /etc/systemd/journald.conf.d
if ! cmp -s "$SYSTEMD_SRC/journald-size-cap.conf" /etc/systemd/journald.conf.d/size-cap.conf 2>/dev/null; then
    cp "$SYSTEMD_SRC/journald-size-cap.conf" /etc/systemd/journald.conf.d/size-cap.conf
    systemctl restart systemd-journald
    echo "Installed journald size cap (journald restarted)"
fi

# The watchdog's service/port/timer registry, rendered from budget.yaml.
#
# This belongs HERE, with the watchdog's own installer, because the manifest is
# box_health.sh's INPUT. It used to live only in install-resource-limits.sh —
# which nothing automated ever calls (not deploy-on-merge.sh, not CI), so a
# budget.yaml change reached the box's git checkout and stopped there. The
# whole point of I4492 was "one list: adding a service to the budget adds it to
# the watchdog", and the propagation step was manual-only, so the two could
# drift indefinitely. Verified live 2026-07-28: config-I5209's timer thresholds
# merged and deployed, and the installed manifest still had none of them.
#
# install-resource-limits.sh keeps its own call (it renders the systemd
# drop-ins from the same file and is still run by hand); both are idempotent
# and produce byte-identical output, so running either is safe.
PY=$(command -v python3)
"$PY" "$REPO_INFRA/generate-box-manifest.py" \
    || { echo "ERROR: manifest generation failed — box_health.sh would fall back to its stale hardcoded list" >&2; exit 1; }

systemctl daemon-reload
systemctl enable box-health.service
systemctl enable --now box-health.timer
systemctl enable box-hygiene.service
systemctl enable --now box-hygiene.timer
systemctl enable box-state-backup.service
systemctl enable --now box-state-backup.timer

echo ""
echo "box-health installed and enabled (runs every 10 min)."
echo "box-hygiene installed and enabled (weekly, Sun 09:20 UTC)."
echo "  Verify:  systemctl list-timers box-health.timer box-hygiene.timer"
echo "  Run now: sudo systemctl start box-health.service"
