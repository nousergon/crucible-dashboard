#!/bin/bash
# reboot_if_needed.sh — reboot the dashboard box only when patching actually
# requires it, in a declared window (alpha-engine-config-I4493).
#
# WHY CONDITIONAL AND NOT SCHEDULED
# ---------------------------------
# `dnf-automatic` applies security updates but cannot reboot conditionally, and
# an unconditional weekly reboot of a box serving eight products buys nothing
# most weeks while spending a real availability event every week. A kernel or
# core-library update is the only thing that needs one, and `needs-restarting -r`
# answers exactly that question.
#
# WHY REBOOTING IS ACCEPTABLE HERE AT ALL
# ---------------------------------------
# Policy condition C2: nothing on this box carries an external availability
# commitment. That is precisely what makes a reboot cheap — and it is also why
# an unpatched kernel is NOT acceptable in exchange. "Uptime" is not a property
# worth protecting on this host; a current kernel is.
#
# WINDOW: Sunday 07:00 UTC (midnight PT Sunday). Chosen to miss everything:
#   - weekday preopen SF        12:15 UTC Mon-Fri
#   - morning-signal/daily-news 11:00 UTC daily
#   - weekly freshness SF       09:00 UTC Saturday
#   - metron-refresh            20:45 / 21:30 / 22:30 UTC
#   - DLM snapshot              08:00 UTC daily
# Sunday is also the fleet's deliberate unscheduled buffer day.
#
# POST-REBOOT VERIFICATION is box_health.timer's job: it runs every 10 minutes
# and now covers all 14 services, all 13 ports, and every timer, so a service
# that fails to come back pages on its own. This script does not try to verify
# what it cannot observe from the other side of a reboot -- it announces intent
# first so the alert exists even if the box never comes back.

set -uo pipefail

VENV_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"
ENV_FILE="/home/ec2-user/alpha-engine-dashboard/.env"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
export AWS_REGION="${AWS_REGION:-us-east-1}"

_tok=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
INSTANCE_ID=$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: ${_tok}" \
    http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "dashboard-ec2")

alert() {
    local sev="$1" msg="$2" key="$3"
    "$VENV_PY" -m krepis.alerts publish \
        --message "$msg" --severity "$sev" --source reboot-if-needed \
        --dedup-key "$key" --dedup-window-min 1440 \
        || echo "reboot_if_needed: alert publish failed" >&2
}

# `needs-restarting -r` exits 0 = no reboot required, 1 = required.
if needs-restarting -r >/dev/null 2>&1; then
    echo "reboot_if_needed: no reboot required (running kernel $(uname -r))"
    exit 0
fi

REASON=$(needs-restarting -r 2>&1 | head -5)
echo "reboot_if_needed: reboot REQUIRED"
echo "$REASON"

# Announce BEFORE rebooting. If the box does not come back, this message is the
# only record that a deliberate reboot -- rather than a crash -- started it.
alert warning \
    "dashboard EC2 (${INSTANCE_ID}) rebooting in its Sunday 07:00 UTC patch window.
Running kernel: $(uname -r)
Reason: ${REASON}
box_health.timer verifies all 14 services, 13 ports and every timer within ~10 min of boot; a service that fails to return will page separately." \
    "reboot-if-needed-${INSTANCE_ID}"

# Give the alert time to leave the box before the network goes away.
sleep 10

systemctl reboot
