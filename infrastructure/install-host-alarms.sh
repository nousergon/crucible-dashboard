#!/bin/bash
# install-host-alarms.sh — CloudWatch alarms for the dashboard box's host metrics.
#
# Idempotent: put-metric-alarm creates or updates in place.
#
# Before 2026-07-27 the only alarms on this box were three on disk_used_percent
# (two of which were duplicates), and none on memory -- on a box whose binding
# constraint is RAM. Two OOM incidents in three days produced zero alerts.
#
# Usage:  ./infrastructure/install-host-alarms.sh
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T0-3

set -euo pipefail

REGION="us-east-1"
NS="AlphaEngine/Host"
INSTANCE="i-09b539c844515d549"
TOPIC="arn:aws:sns:us-east-1:711398986525:alpha-engine-alerts"
DIM="Name=InstanceId,Value=${INSTANCE}"

alarm() {
    local name="$1" metric="$2" op="$3" threshold="$4" period="$5" evals="$6" desc="$7"
    aws cloudwatch put-metric-alarm \
        --region "$REGION" \
        --alarm-name "$name" \
        --alarm-description "$desc" \
        --namespace "$NS" \
        --metric-name "$metric" \
        --dimensions "$DIM" \
        --statistic Average \
        --period "$period" \
        --evaluation-periods "$evals" \
        --threshold "$threshold" \
        --comparison-operator "$op" \
        --treat-missing-data missing \
        --alarm-actions "$TOPIC" \
        --ok-actions "$TOPIC"
    echo "  ok  $name"
}

echo "==> memory (the binding constraint on this box)"
alarm alpha-engine-dashboard-mem-available-warn mem_available_percent \
    LessThanThreshold 15 60 3 \
    "Dashboard box MemAvailable below 15% for 3 min. Precursor to the 2026-07-27 cascade."

alarm alpha-engine-dashboard-mem-available-crit mem_available_percent \
    LessThanThreshold 8 60 2 \
    "Dashboard box MemAvailable below 8% for 2 min. At this level on 2026-07-27 the SSM agent was starved and the box could not be managed remotely."

echo "==> OOM kills"
# OOMKills is a DELTA (see emit_oom_metric.sh), so any nonzero datapoint is a
# NEW kill and the alarm self-clears on the next healthy sample. A cumulative
# counter would latch this on forever after the first kill.
# treat-missing-data=notBreaching so a gap does not page; the dead-man check
# for the collector itself belongs with the timer monitoring in I4487.
aws cloudwatch put-metric-alarm \
    --region "$REGION" \
    --alarm-name alpha-engine-dashboard-oom-kill \
    --alarm-description "A unit on the dashboard box was killed by the OOM killer. Check 'journalctl -u emit-oom-metric' for the unit name." \
    --namespace "$NS" \
    --metric-name OOMKills \
    --dimensions "$DIM" \
    --statistic Maximum \
    --period 300 \
    --evaluation-periods 1 \
    --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC" \
    --ok-actions "$TOPIC"
echo "  ok  alpha-engine-dashboard-oom-kill"

echo "==> swap"
# Threshold is 75%, not 50%, deliberately. Linux does not proactively page
# swapped-out memory back in when pressure subsides -- after the 2026-07-27
# incident the box sat at ~50% swap while holding 1.8 GB available, entirely
# healthy. Swap occupancy alone is therefore a weak signal; mem_available is
# the real one. This alarm exists to catch sustained heavy swapping, not
# residue.
alarm alpha-engine-dashboard-swap-used-warn swap_used_percent \
    GreaterThanThreshold 75 60 5 \
    "Dashboard box swap above 75% for 5 min. Note residual swap after a pressure event is normal; treat mem_available as the primary signal."

echo "==> disk (migrated from the AlphaEngine/HostDisk namespace)"
# Disk carries THREE dimensions, not one. `drop_device: true` in the agent
# config removes only `device`; `path` and `fstype` remain, and a CloudWatch
# alarm must match the metric's dimension set EXACTLY. An alarm on InstanceId
# alone matches nothing and sits in INSUFFICIENT_DATA forever -- which looks
# like "no data yet" rather than "misconfigured", so it is easy to miss.
# (Caught here 2026-07-27 by checking datapoints rather than trusting the
# alarm's own state.)
DISK_DIMS="Name=InstanceId,Value=${INSTANCE} Name=path,Value=/ Name=fstype,Value=xfs"

disk_alarm() {
    local name="$1" threshold="$2" desc="$3"
    aws cloudwatch put-metric-alarm \
        --region "$REGION" \
        --alarm-name "$name" \
        --alarm-description "$desc" \
        --namespace "$NS" \
        --metric-name disk_used_percent \
        --dimensions $DISK_DIMS \
        --statistic Average \
        --period 300 \
        --evaluation-periods 1 \
        --threshold "$threshold" \
        --comparison-operator GreaterThanOrEqualToThreshold \
        --treat-missing-data missing \
        --alarm-actions "$TOPIC" \
        --ok-actions "$TOPIC"
    echo "  ok  $name"
}

disk_alarm alpha-engine-dashboard-disk-warn 80 \
    "Dashboard box root filesystem at/above 80%."

disk_alarm alpha-engine-dashboard-disk-crit 90 \
    "Dashboard box root filesystem at/above 90%."

echo
echo "Legacy disk alarms in AlphaEngine/HostDisk are NOT deleted by this script."
echo "Delete them only after confirming the new ones have data and are in OK:"
echo "  alpha-engine-dashboard-box-disk-critical"
echo "  alpha-engine-disk-dashboard-crit"
echo "  alpha-engine-disk-dashboard-warn"
