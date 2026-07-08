#!/bin/bash
# install-morning-signal-bakeoff.sh — Installer for the weekly OSS Phase B
# shadow-bakeoff systemd footprint (config#1659) on the shared dashboard EC2.
#
# Same pattern as install-morning-signal.sh / install-morning-signal-watchdog.sh
# — units live in this repo's infrastructure/systemd/ so a rebuild doesn't
# lose them, and deploy-on-merge.sh re-runs this automatically when any of
# these files change in a merge.
#
# Idempotent — re-run to update. Must run as root via sudo.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-morning-signal-bakeoff.sh

set -euo pipefail

REPO_INFRA="/home/ec2-user/alpha-engine-dashboard/infrastructure"
SYSTEMD_SRC="$REPO_INFRA/systemd"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi
if [ ! -f "$SYSTEMD_SRC/morning-signal-bakeoff.service" ]; then
    echo "ERROR: $SYSTEMD_SRC/morning-signal-bakeoff.service not found — pull alpha-engine-dashboard first" >&2
    exit 1
fi

for unit in morning-signal-bakeoff.service morning-signal-bakeoff.timer; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable --now morning-signal-bakeoff.timer

echo ""
echo "morning-signal-bakeoff units installed; timer enabled (Wed 05:00 PT weekly)."
echo "  Verify:    systemctl list-timers morning-signal-bakeoff.timer"
echo "  Run now:   sudo systemctl start morning-signal-bakeoff.service"
echo "  Logs:      journalctl -u morning-signal-bakeoff.service -n 100"
