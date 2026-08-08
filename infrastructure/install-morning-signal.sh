#!/bin/bash
# install-morning-signal.sh — Installer for the core morning-signal systemd
# footprint on the shared dashboard EC2 (service + timer + drop-ins) and the
# generate-only recovery wrapper.
#
# These units were previously created ad-hoc on the box and lived in NO repo
# (morning-signal#79) — so edits couldn't be reviewed and a box rebuild lost
# them. They now live in this repo's infrastructure/systemd/ alongside the
# watchdog + box-health units (same box, same deploy-on-merge fast-path).
#
# Idempotent — re-run to update. Must run as root via sudo. deploy-on-merge.sh
# re-runs this automatically when any of these files change in a merge.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-morning-signal.sh

set -euo pipefail

REPO_INFRA="/home/ec2-user/alpha-engine-dashboard/infrastructure"
SYSTEMD_SRC="$REPO_INFRA/systemd"
DROPIN_DST="/etc/systemd/system/morning-signal.service.d"
RECOVER_SRC="$REPO_INFRA/morning-signal-recover.sh"
RECOVER_DST="/usr/local/bin/morning-signal-recover.sh"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi
if [ ! -f "$SYSTEMD_SRC/morning-signal.service" ]; then
    echo "ERROR: $SYSTEMD_SRC/morning-signal.service not found — pull alpha-engine-dashboard first" >&2
    exit 1
fi

for unit in morning-signal.service morning-signal.timer; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

install -d -m 0755 "$DROPIN_DST"
# 10-timeout.conf and 20-router.conf were applied by hand to the live box
# on 2026-08-08 and existed in no repo until now — exactly the state this
# installer's own header says these units used to be in. A drop-in absent
# from this list is NOT installed on a rebuilt box, and the unit that
# comes up is a different unit.
for conf in 10-after-news.conf 10-memory.conf 10-timeout.conf 20-router.conf; do
    cp "$SYSTEMD_SRC/morning-signal.service.d/$conf" "$DROPIN_DST/$conf"
    echo "Installed $DROPIN_DST/$conf"
done

install -m 0755 "$RECOVER_SRC" "$RECOVER_DST"
echo "Installed $RECOVER_DST"

systemctl daemon-reload
systemctl enable --now morning-signal.timer

echo ""
echo "morning-signal core units installed; timer enabled (04:00 PT daily)."
echo "  Verify:    systemctl list-timers morning-signal.timer"
echo "  Recover:   sudo -u ec2-user bash $RECOVER_DST   # generate-only, skips daily-news"
