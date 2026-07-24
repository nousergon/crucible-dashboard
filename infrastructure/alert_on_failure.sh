#!/bin/bash
# alert_on_failure.sh — generic systemd OnFailure= handler (config#2954).
#
# Invoked as `alert-on-failure@<unit>.service`'s ExecStart with the failed
# unit's name (systemd's %i, already unescaped by the template) as $1.
# Pages via krepis.alerts from THIS box's own instance role — independent
# of whatever credential/identity path the failed unit itself uses, so a
# failure IN that path still pages (same principle box_health.sh /
# morning-signal-watchdog.sh already rely on for their own alerting).
#
# Reusable: any oneshot unit that sets `OnFailure=alert-on-failure@%n.service`
# gets a page on failure without writing its own alerting path. First
# consumer: substrate-health-daily.service.
#
# Installed as a unit template by install-substrate-health-daily.sh.
set -uo pipefail

UNIT="${1:?usage: alert_on_failure.sh <failed-unit-name>}"

ENV_FILE="/home/ec2-user/.alpha-engine.env"
VENV_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"

# Load Telegram creds etc. (SNS auth comes from the instance role) — same
# convention as box_health.sh.
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export AWS_REGION="${AWS_REGION:-us-east-1}"

day="$(date -u +%F)"
# journalctl, not `systemctl status`: status can itself fail to run under a
# minimal OnFailure= handler PATH, and the journal is the more direct "why
# did it fail" evidence for the page. Best-effort — an empty detail still
# pages (the unit-failed fact alone is actionable).
detail="$(journalctl -u "$UNIT" -n 30 --no-pager 2>&1 || true)"

# Per-unit-per-day dedup key → a persistently failing unit pages once a
# day, not on every retry/timer firing.
"$VENV_PY" -m krepis.alerts publish \
    --message "🚨 ${UNIT} failed on $(hostname) (${day} UTC). Last 30 journal lines:"$'\n'"${detail}" \
    --severity warning \
    --source "${UNIT%.service}-onfailure" \
    --dedup-key "onfailure-${UNIT}-${day}" \
    --dedup-window-min 360 \
    || echo "alert_on_failure: publish failed for $UNIT" >&2

# Exit 0 like box_health.sh / morning-signal-watchdog.sh: this handler did
# its job (detected + paged) — its own unit staying green avoids a second
# OnFailure loop on itself.
exit 0
