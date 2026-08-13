#!/bin/bash
# morning-signal-watchdog.sh — verify today's morning-signal episode landed in S3.
#
# WHY: the morning-signal generate run can fail SILENTLY. Its in-process
# flow-doctor guard only reports exceptions raised *inside* the run body, but
# the failure modes that page nobody happen earlier or outside the process:
#   - a bootstrap/AssumeRole/SSM failure (the creds for the Telegram notifier
#     load DURING bootstrap, so a failure there reports to no one — this is
#     exactly what happened 2026-06-10 when the box instance role changed);
#   - the generate timer never firing;
#   - an OOM kill.
#
# This wrapper runs `morning-signal watchdog` (which checks the *deliverable* —
# is today's episode present + fresh in S3). On ANY non-zero exit, including a
# bootstrap failure, it alerts via krepis.alerts from THIS box's own
# identity (instance role), which is INDEPENDENT of morning-signal-runner-role —
# so a runner-role break (the silent class) still pages. Mirrors box_health.sh.
#
# Installed to /usr/local/bin by install-morning-signal-watchdog.sh; scheduled
# by morning-signal-watchdog.timer (06:15 PT, after the 05:00 PT generate slot).
set -uo pipefail

# Alerts publish through the DECLARED krepis venv, not through whichever
# checkout happened to carry a krepis (config-I7168).
#
# TWO candidate paths, because three of these scripts are INSTALLED to
# /usr/local/bin and run from there, where no sibling exists. Resolving only
# alongside $BASH_SOURCE is what broke box-health within a minute of deploying
# I7168: `/usr/local/bin/box_health.sh: line 1215: ALERT_PY: unbound variable`,
# every 10 minutes, on the box's primary watchdog. The file was correct; the
# DEPLOY PATH was not, and no amount of reading the file shows that.
#
# The final `:=` is the load-bearing line. Whatever happens above it, ALERT_PY
# is set — an alerting path that cannot start is strictly worse than one on an
# older krepis, and `set -u` turns an unset variable into a dead watchdog.
for _ap in "$(dirname "${BASH_SOURCE[0]}")/alert_py.sh" \
           /home/ec2-user/alpha-engine-dashboard/infrastructure/alert_py.sh; do
    if [ -r "$_ap" ]; then . "$_ap"; break; fi
done
unset _ap
: "${ALERT_PY:=/home/ec2-user/alpha-engine-dashboard/.venv/bin/python}"

ENV_FILE="/home/ec2-user/.alpha-engine.env"
DASH_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"
MS_BIN="/home/ec2-user/morning-signal/.venv/bin/morning-signal"

# Telegram creds for the alert come from the env file (NOT the runner role).
# SNS auth comes from this box's instance role.
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export AWS_REGION="${AWS_REGION:-us-east-1}"

# .alpha-engine.env ships STATIC creds for the cipher813 IAM *user*, but that
# user is NOT a principal in morning-signal-runner-role's trust policy — only
# this box's instance role (alpha-engine-dashboard-role) is. Drop the user creds
# so boto3 falls back to the instance role: the watchdog's AssumeRole then
# succeeds, and krepis.alerts' SNS publish still works (the instance
# role holds alpha-engine-sns-publish). Telegram uses its bot token, not AWS.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# Same runtime env the generate service uses, so the check assumes the runner
# role + reads SSM identically. If that assume fails, the watchdog exits
# non-zero and we alert below from the independent identity.
export MORNING_SIGNAL_RUNNER_ROLE_ARN="arn:aws:iam::711398986525:role/morning-signal-runner-role"
export MORNING_SIGNAL_USE_SSM=1
export MORNING_SIGNAL_SSM_REGION=us-east-1

day="$(TZ=America/Los_Angeles date +%F)"

# Pin --edition am: production ships a SINGLE 5 AM edition (PM dropped
# 2026-06-04). The CLI otherwise infers the edition from the clock and would
# check a non-existent {date}-pm.mp3 on any afternoon run — e.g. a Persistent=true
# catch-up firing after the box was down at 06:15 — and false-alarm.
out="$("$MS_BIN" watchdog --edition am 2>&1)"; rc=$?
if [ "$rc" -eq 0 ]; then
    echo "morning-signal-watchdog: OK ($day) — $out"
    exit 0
fi

# Episode missing/stale OR the check itself couldn't run (bootstrap failure).
echo "morning-signal-watchdog: FAIL ($day, rc=$rc)" >&2
echo "$out" >&2

# Per-day dedup key → a persistent miss pages once for the day, not per run.
# krepis.alerts is the canonical CLI (config#1649): nousergon_lib.alerts is a
# re-export shim since lib v0.66.0 — guard-less under `python -m` on 0.81.0
# (silent exit-0 no-op, the config#1646 class). Invoke the real module.
"$ALERT_PY" -m krepis.alerts publish \
    --message "🚨 Morning Signal: today's episode ($day) did NOT publish (watchdog exit=$rc). Detail: $out" \
    --severity warning \
    --source morning-signal-watchdog \
    --dedup-key "ms-watchdog-$day" \
    --dedup-window-min 360 \
    || echo "morning-signal-watchdog: alert publish failed" >&2

# Exit 0 like box_health.sh: the watchdog did its job (detected + paged). The
# ALERT is the signal, not the unit state — so the timer stays green and other
# monitors don't double-fire on a "failed" unit.
exit 0
