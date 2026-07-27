#!/bin/bash
# emit_oom_metric.sh — publish a DELTA count of cgroup OOM kills to CloudWatch.
#
# The CloudWatch agent has no OOM-kill metric, and OOM kills are the single
# failure mode this box actually suffers. Two in three days went unnoticed:
# metron-refresh on 2026-07-25 (dead for two days, silently), and the cascade
# on 2026-07-27 where services cycled while nothing recorded it.
#
# WHY A DELTA, NOT THE RAW COUNTER
# --------------------------------
# /sys/fs/cgroup/**/memory.events exposes `oom_kill` as a CUMULATIVE counter
# that only resets when the cgroup is recreated. Publishing it raw means an
# alarm on "> 0" fires forever after the first kill and has to be manually
# reset -- which is how alarms get ignored. Publishing the delta since the last
# run means ANY nonzero datapoint is a NEW kill, so the alarm is
# self-clearing and every firing is real.
#
# The state file survives reboots (it lives under /var/lib). On a cgroup reset
# the current total can go BACKWARDS relative to the stored value; that is
# treated as a fresh baseline (delta 0), never as a negative.
#
# Emits, to namespace AlphaEngine/Host, dimension InstanceId:
#   OOMKills      — kills since the previous run (0 on a healthy box)
#   OOMKillsTotal — cumulative, for context when investigating
#
# Runs every 5 min via emit-oom-metric.timer.
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T0-3.

set -uo pipefail

STATE_DIR="/var/lib/alpha-engine"
STATE_FILE="${STATE_DIR}/oom_kill_count"
NAMESPACE="AlphaEngine/Host"
REGION="us-east-1"

mkdir -p "$STATE_DIR"

# Sum oom_kill across every cgroup that has a memory.events file. Covers
# system.slice services and any nested scopes. `memory.events` reports kills
# for the cgroup and its descendants, so summing every level would double
# count -- we read only the per-service level under system.slice.
total=0
shopt -s nullglob
for f in /sys/fs/cgroup/system.slice/*/memory.events \
         /sys/fs/cgroup/system.slice/*.service/memory.events; do
    n=$(awk '$1=="oom_kill"{print $2}' "$f" 2>/dev/null)
    [[ -n "${n:-}" ]] && total=$(( total + n ))
done
shopt -u nullglob

prev=0
[[ -f "$STATE_FILE" ]] && prev=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
[[ "$prev" =~ ^[0-9]+$ ]] || prev=0

delta=$(( total - prev ))
# A negative delta means cgroups were recreated (reboot, unit reinstall).
# Re-baseline rather than reporting nonsense.
(( delta < 0 )) && delta=0

printf '%s\n' "$total" > "$STATE_FILE"

TOKEN=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
INSTANCE_ID=$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")

# Fail loud: a metric publisher that silently swallows its own failure is the
# same defect class this script exists to fix (see boot-pull's broken reporter,
# alpha-engine-config-I4509). Exit non-zero so systemd records the failure.
if ! aws cloudwatch put-metric-data \
        --region "$REGION" \
        --namespace "$NAMESPACE" \
        --metric-data \
            "MetricName=OOMKills,Value=${delta},Unit=Count,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}]" \
            "MetricName=OOMKillsTotal,Value=${total},Unit=Count,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}]"
then
    echo "emit_oom_metric: put-metric-data FAILED (delta=${delta} total=${total})" >&2
    exit 1
fi

if (( delta > 0 )); then
    echo "emit_oom_metric: ${delta} NEW oom kill(s) since last run (cumulative ${total})"
    # Name the culprits -- the metric says "something died", the log says what.
    for f in /sys/fs/cgroup/system.slice/*.service/memory.events; do
        n=$(awk '$1=="oom_kill"{print $2}' "$f" 2>/dev/null)
        [[ -n "${n:-}" ]] && (( n > 0 )) && echo "  $(basename "$(dirname "$f")"): ${n} total"
    done
else
    echo "emit_oom_metric: no new oom kills (cumulative ${total})"
fi
