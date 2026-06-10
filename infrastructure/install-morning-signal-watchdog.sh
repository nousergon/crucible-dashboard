#!/bin/bash
# install-morning-signal-watchdog.sh — One-time installer for the morning-signal
# episode freshness watchdog on the shared dashboard EC2.
#
# Copies morning-signal-watchdog.sh -> /usr/local/bin (a non-repo path, so the
# box's git working tree never collides with the live script), installs the
# morning-signal-watchdog.service + .timer units, and enables the timer
# (06:15 PT daily). Must run as root via sudo. Idempotent — re-run to update.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-morning-signal-watchdog.sh

set -euo pipefail

REPO_INFRA="/home/ec2-user/alpha-engine-dashboard/infrastructure"
SCRIPT_SRC="$REPO_INFRA/morning-signal-watchdog.sh"
SCRIPT_DST="/usr/local/bin/morning-signal-watchdog.sh"
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

for unit in morning-signal-watchdog.service morning-signal-watchdog.timer; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable morning-signal-watchdog.service
systemctl enable --now morning-signal-watchdog.timer

echo ""
echo "morning-signal-watchdog installed and enabled (runs 06:15 PT daily)."
echo "  Verify:  systemctl list-timers morning-signal-watchdog.timer"
echo "  Run now: sudo systemctl start morning-signal-watchdog.service && journalctl -u morning-signal-watchdog.service -n 20 --no-pager"
