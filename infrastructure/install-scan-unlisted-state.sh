#!/bin/bash
# install-scan-unlisted-state.sh — one-time installer for the unlisted-state
# scan (T1-4, alpha-engine-config-I6719).
#
# Installs scan-unlisted-state.service + .timer. Idempotent — re-run to apply
# updated files. Routed via deploy-on-merge.sh's ROUTED_INSTALLERS table
# (files mode), so a merge alone re-installs it when either unit changes —
# no operator step needed for ordinary updates. Must run as root via sudo.
#
# Usage:
#   sudo bash /home/ec2-user/alpha-engine-dashboard/infrastructure/install-scan-unlisted-state.sh

set -euo pipefail

REPO_INFRA="/home/ec2-user/alpha-engine-dashboard/infrastructure"
SYSTEMD_SRC="$REPO_INFRA/systemd"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi
if [ ! -f "$REPO_INFRA/scan_unlisted_state.py" ]; then
    echo "ERROR: $REPO_INFRA/scan_unlisted_state.py not found — pull alpha-engine-dashboard first" >&2
    exit 1
fi

for unit in scan-unlisted-state.service scan-unlisted-state.timer; do
    cp "$SYSTEMD_SRC/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable scan-unlisted-state.service
systemctl enable --now scan-unlisted-state.timer

echo ""
echo "scan-unlisted-state installed and enabled (daily, 04:20 UTC)."
echo "  Verify:  systemctl list-timers scan-unlisted-state.timer"
echo "  Run now: sudo systemctl start scan-unlisted-state.service"
echo "  Findings: journalctl -u scan-unlisted-state.service -n 50"
