#!/bin/bash
# install-substrate-health-daily.sh — One-time installer for the daily
# substrate health check, re-homed off ne-postclose-trading-pipeline's
# DailySubstrateHealthCheck SF chain (alpha-engine-config-I2722).
#
# Mirrors install-daily-news.sh's shape: the service ExecStart points
# directly at the script's REPO path (not a /usr/local/bin copy), because
# — like daily-news — this script self-refreshes via its own `git pull`
# at the top of every run, so there's no separate repo-vs-live-script
# staleness concern to guard against (unlike box-health/box-hygiene, which
# never pull and are copied out to /usr/local/bin for that reason). This
# script only handles the SEPARATE concern of the unit FILES landing in
# /etc/systemd/system/, which a plain code pull never touches. Must run as
# root via sudo. Idempotent — re-run to apply updated unit files.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-substrate-health-daily.sh
set -euo pipefail

REPO_INFRA="/home/ec2-user/alpha-engine-dashboard/infrastructure"
SCRIPT_SRC="$REPO_INFRA/substrate_health_check_daily.sh"
ALERT_SCRIPT_SRC="$REPO_INFRA/alert_on_failure.sh"
SYSTEMD_SRC="$REPO_INFRA/systemd"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi
if [ ! -f "$SCRIPT_SRC" ]; then
    echo "ERROR: $SCRIPT_SRC not found — pull alpha-engine-dashboard first" >&2
    exit 1
fi
if [ ! -f "$ALERT_SCRIPT_SRC" ]; then
    echo "ERROR: $ALERT_SCRIPT_SRC not found — pull alpha-engine-dashboard first" >&2
    exit 1
fi

chmod 0755 "$SCRIPT_SRC" "$ALERT_SCRIPT_SRC"

# config#2954: alert-on-failure@.service is a reusable TEMPLATE unit (not
# enabled/started directly — systemd instantiates + starts it on demand via
# substrate-health-daily.service's OnFailure=), so it's installed alongside
# the concrete units but not passed to `systemctl enable`.
for unit in substrate-health-daily.service substrate-health-daily.timer "alert-on-failure@.service"; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable substrate-health-daily.service
systemctl enable --now substrate-health-daily.timer

echo ""
echo "substrate-health-daily installed and enabled (Mon-Fri 22:30 UTC)."
echo "  Verify:       systemctl list-timers substrate-health-daily.timer"
echo "  Run now:      sudo systemctl start substrate-health-daily.service"
echo "  Test paging:  sudo systemctl start alert-on-failure@substrate-health-daily.service.service"
