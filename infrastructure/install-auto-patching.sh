#!/bin/bash
# install-auto-patching.sh — automated security patching + conditional reboot
# for the dashboard box (alpha-engine-config-I4493).
#
# Before this, the box had no automated patching and 53 days of uptime. Its
# package set happened to be current, which is what made the gap easy to miss:
# patching was incidental, not guaranteed, and nothing alerted when it lapsed.
#
# Usage:  sudo ./infrastructure/install-auto-patching.sh
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T1-5

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v needs-restarting >/dev/null || {
    echo "needs-restarting missing (provided by dnf-utils) — install it first" >&2
    exit 1; }

echo "==> installing dnf-automatic"
rpm -q dnf-automatic >/dev/null 2>&1 || dnf install -y dnf-automatic

echo "==> installing security-only config"
install -m 0644 "$HERE/dnf-automatic.conf" /etc/dnf/automatic.conf

echo "==> wiring failure alerting for dnf-automatic"
install -d /etc/systemd/system/dnf-automatic.service.d
install -m 0644 "$HERE/systemd/dnf-automatic-alert.conf" \
    /etc/systemd/system/dnf-automatic.service.d/10-alert.conf

echo "==> installing conditional reboot job"
install -m 0755 "$HERE/reboot_if_needed.sh" /usr/local/bin/reboot_if_needed.sh
install -m 0644 "$HERE/systemd/reboot-if-needed.service" /etc/systemd/system/
install -m 0644 "$HERE/systemd/reboot-if-needed.timer"   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now dnf-automatic.timer
systemctl enable --now reboot-if-needed.timer

echo
echo "Enabled:"
systemctl list-timers dnf-automatic.timer reboot-if-needed.timer --no-pager || true
echo
echo "Reboot window is Sunday 07:00 UTC, and fires ONLY when"
echo "\`needs-restarting -r\` says a reboot is actually required."
echo
echo "Dry-run the reboot check without rebooting:"
echo "  needs-restarting -r; echo \"exit=\$? (0 = no reboot needed)\""
