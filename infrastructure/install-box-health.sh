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

for unit in box-health.service box-health.timer box-hygiene.service box-hygiene.timer; do
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

systemctl daemon-reload
systemctl enable box-health.service
systemctl enable --now box-health.timer
systemctl enable box-hygiene.service
systemctl enable --now box-hygiene.timer

echo ""
echo "box-health installed and enabled (runs every 10 min)."
echo "box-hygiene installed and enabled (weekly, Sun 09:20 UTC)."
echo "  Verify:  systemctl list-timers box-health.timer box-hygiene.timer"
echo "  Run now: sudo systemctl start box-health.service"
