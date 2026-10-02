#!/bin/bash
# install-boot-pull.sh — One-time installer for the micro's daily code-pull.
#
# Installs boot-pull.service (oneshot) + boot-pull.timer (daily @ 12:00 UTC)
# and enables both. Must be run as root via sudo.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-boot-pull.sh
#
# Idempotent — safe to run multiple times. Re-copies unit files from the
# repo each run, so pulling updates to the unit files and re-running this
# script applies them.
#
# After install, subsequent updates to the unit files are picked up
# automatically by boot-pull.sh itself (it syncs from repo → systemd on
# each run), so you only run this script once.

set -euo pipefail

SCRIPT="/home/ec2-user/alpha-engine-dashboard/infrastructure/boot-pull.sh"
LAUNCHER_SRC="/home/ec2-user/alpha-engine-dashboard/infrastructure/boot-pull-launcher.sh"
LAUNCHER_DST="/usr/local/sbin/boot-pull-launcher.sh"
SYSTEMD_SRC="/home/ec2-user/alpha-engine-dashboard/infrastructure/systemd"
LOG="/var/log/boot-pull.log"

# Preconditions
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi

if [ ! -f "$SCRIPT" ]; then
    echo "ERROR: boot-pull.sh not found at $SCRIPT" >&2
    echo "       Pull alpha-engine-dashboard first: git -C /home/ec2-user/alpha-engine-dashboard pull" >&2
    exit 1
fi

# Ensure log file exists with correct ownership
touch "$LOG"
chown ec2-user:ec2-user "$LOG"

# Ensure script is executable
chmod +x "$SCRIPT"

# alpha-engine-config-I8734: boot-pull.service ExecStarts the launcher at
# $LAUNCHER_DST, OUTSIDE the checkout boot-pull.sh syncs. Install it BEFORE the
# unit that names it, and prove it is executable — a unit copied over a
# missing launcher fails 203/EXEC every hour with nothing left to repair it.
# set -e stops here on failure, so the unit copy below never runs.
install -m 0755 -o root -g root "$LAUNCHER_SRC" "$LAUNCHER_DST"
if [ ! -x "$LAUNCHER_DST" ]; then
    echo "ERROR: $LAUNCHER_DST is not executable after install — not installing boot-pull.service" >&2
    exit 1
fi
echo "Installed $LAUNCHER_DST"

# Copy unit files from the repo to systemd
for unit in boot-pull.service boot-pull.timer; do
    src="$SYSTEMD_SRC/$unit"
    dst="/etc/systemd/system/$unit"
    if [ ! -f "$src" ]; then
        echo "ERROR: $src missing — did alpha-engine-dashboard pull succeed?" >&2
        exit 1
    fi
    cp "$src" "$dst"
    echo "Installed $dst"
done

systemctl daemon-reload
systemctl enable boot-pull.service
systemctl enable boot-pull.timer
systemctl start boot-pull.timer

echo ""
echo "boot-pull installed and enabled."
echo ""
echo "  Schedule:  daily at 12:00 UTC (5am PDT / 4am PST)"
echo "  Log file:  $LOG"
echo ""
echo "  Verify timer is active:"
echo "    systemctl list-timers boot-pull.timer"
echo ""
echo "  Run it right now to validate:"
echo "    sudo systemctl start boot-pull && sleep 30 && tail -30 $LOG"
