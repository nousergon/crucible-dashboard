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

for unit in box-health.service box-health.timer; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable box-health.service
systemctl enable --now box-health.timer

echo ""
echo "box-health installed and enabled (runs every 10 min)."
echo "  Verify:  systemctl list-timers box-health.timer"
echo "  Run now: sudo systemctl start box-health.service"
