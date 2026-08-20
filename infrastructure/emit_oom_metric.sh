#!/bin/bash
# emit_oom_metric.sh — publish a count of NEW OOM kills to CloudWatch.
#
# The CloudWatch agent has no OOM-kill metric, and OOM kills are the failure
# mode this box actually suffers. Two in three days went unnoticed:
# metron-refresh on 2026-07-25 (dead for two days, silently) and the cascade
# on 2026-07-27 where services cycled while nothing recorded it.
#
# WHY THE JOURNAL AND NOT /sys/fs/cgroup/**/memory.events
# -------------------------------------------------------
# The obvious implementation -- sum `oom_kill` from each service's
# memory.events -- is WRONG, and was caught failing its own acceptance test on
# 2026-07-27: a deliberately OOM-killed canary unit produced
# `systemctl show -p Result` = `oom-kill` while the cgroup sum still read 0.
#
# The reason is that a cgroup is DESTROYED when its unit goes away, and
# memory.events goes with it. So the cgroup approach only ever sees kills in
# units that are still loaded -- it structurally cannot see a kill in a
# oneshot, a transient `systemd-run` unit, or anything already cleaned up.
# Oneshot batch jobs are precisely the case that motivated this collector
# (metron-refresh is `Type=oneshot`), so the cgroup approach would have missed
# the very incident it was built to catch.
#
# The journal is durable across unit teardown and reboots. systemd emits one
# line per unit OOM kill:
#   `<unit>: A process of this unit has been killed by the OOM killer.`
#
# CURSOR, NOT TIMESTAMP
# ---------------------
# State is a journal CURSOR, not a clock reading. A timestamp window either
# double-counts or drops events around the boundary depending on rounding and
# clock skew; a cursor resumes exactly where the last run stopped. If the
# cursor is stale/invalid (log rotation, journal reset) we fall back to the
# current boot and re-baseline rather than replaying history as "new".
#
# Emits, to namespace AlphaEngine/Host, dimension InstanceId:
#   OOMKills — kills since the previous run. 0 on a healthy box, so ANY
#              nonzero datapoint is a NEW kill and the alarm self-clears.
#              (A cumulative counter would latch an alarm on forever after the
#              first kill, which is how alarms get ignored.)
#
# Runs every 5 min via emit-oom-metric.timer.
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T0-3.

set -uo pipefail

STATE_DIR="/var/lib/alpha-engine"
CURSOR_FILE="${STATE_DIR}/oom_journal_cursor"
NAMESPACE="AlphaEngine/Host"
REGION="us-east-1"

# systemd's per-unit OOM line. Matching systemd's message rather than the
# kernel's `Out of memory: Killed process` avoids double-counting a single
# kill that produces both, and gives us the unit name for the log line below.
PATTERN='killed by the OOM killer'

mkdir -p "$STATE_DIR"

# _PID=1 restricts the scan to systemd's OWN messages, and that restriction is
# load-bearing, not tidiness.
#
# Without it this script reads its own output: it logs the matched OOM lines to
# the journal (so the alert names the dead unit), those log lines contain the
# match pattern verbatim, and the next run counts them as fresh kills. Observed
# live 2026-07-27 — one real kill at 16:17:55 produced "1 NEW oom kill" every
# 5 minutes indefinitely, each log entry nesting the previous one, holding the
# CloudWatch alarm permanently in ALARM. That is exactly the latched-alarm
# failure the delta design exists to prevent, reintroduced through the back
# door.
#
# systemd (PID 1) is also the authoritative emitter of the per-unit line
# `<unit>: A process of this unit has been killed by the OOM killer.`, so
# narrowing to it loses no real signal.
read_since_cursor() {
    local cursor="$1"
    journalctl _PID=1 --after-cursor="$cursor" --no-pager --output=short 2>/dev/null
}

if [[ -s "$CURSOR_FILE" ]] && lines=$(read_since_cursor "$(cat "$CURSOR_FILE")") ; then
    :
else
    # No cursor yet, or the cursor no longer resolves. Baseline from this boot
    # WITHOUT counting anything -- replaying old kills as "new" would fire a
    # false alarm on every journal reset.
    lines=""
    echo "emit_oom_metric: no usable cursor; re-baselining from current position" >&2
fi

delta=$(printf '%s' "$lines" | grep -c "$PATTERN" || true)
[[ "$delta" =~ ^[0-9]+$ ]] || delta=0

# Advance the cursor to the journal's current end, whether or not we counted
# anything, so the next run resumes from here.
new_cursor=$(journalctl _PID=1 -n 0 --show-cursor --no-pager 2>/dev/null \
             | sed -n 's/^-- cursor: //p' | tail -1)
if [[ -n "$new_cursor" ]]; then
    printf '%s\n' "$new_cursor" > "$CURSOR_FILE"
else
    echo "emit_oom_metric: could not read journal cursor" >&2
    exit 1
fi

# THE INSTANCE ID IS A REQUIRED DIMENSION, AND `curl` EXITS 0 WITHOUT ONE.
#
# MEASURED 2026-08-20: this timer failed at 10:21 and 14:01 UTC with
#   Error parsing parameter '--metric-data': Expected: ',', received: ']'
#    MetricName=OOMKills,Value=0,Unit=Count,Dimensions=[{Name=InstanceId,Value=}]
# — an EMPTY instance id, not the "unknown" the old `|| echo` fallback
# promised. The fallback never fires: when the token PUT times out at
# --max-time 2, TOKEN is empty, IMDSv2 answers the follow-up GET with a 401,
# and `curl -s` WITHOUT `-f` treats an HTTP error as success — exit 0, empty
# body. So `|| echo "unknown"` guards the one case that does not happen and
# misses the one that does.
#
# Three changes, each load-bearing:
#   -f          make an HTTP error an exit failure, so the guards below see it
#   retry       IMDS timeouts here are transient (both failing runs sat between
#               runs that succeeded a few minutes either side); one retry turns
#               a page into a non-event
#   shape check refuse to publish a dimension that is not an instance id, and
#               say which of the two hops failed. "unknown" is worse than
#               failing: it publishes a real datapoint under a fake dimension,
#               so the OOMKills series silently forks into a second stream that
#               no alarm watches.
fetch_instance_id() {
    local token
    token=$(curl -sf --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null) || return 1
    [[ -n "$token" ]] || return 1
    curl -sf --max-time 2 -H "X-aws-ec2-metadata-token: ${token}" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null
}

INSTANCE_ID=""
for _attempt in 1 2 3; do
    INSTANCE_ID=$(fetch_instance_id || true)
    [[ "$INSTANCE_ID" =~ ^i-[0-9a-f]+$ ]] && break
    INSTANCE_ID=""
    sleep 1
done

if [[ -z "$INSTANCE_ID" ]]; then
    echo "emit_oom_metric: IMDS did not yield an instance id after 3 attempts --" \
         "refusing to publish OOMKills under an empty or fake dimension (delta=${delta})" >&2
    exit 1
fi

# Fail loud: a metric publisher that silently swallows its own failure is the
# same defect class this exists to fix (see boot-pull's broken reporter,
# alpha-engine-config-I4509). Exit non-zero so systemd records it.
if ! aws cloudwatch put-metric-data \
        --region "$REGION" \
        --namespace "$NAMESPACE" \
        --metric-data \
            "MetricName=OOMKills,Value=${delta},Unit=Count,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}]"
then
    echo "emit_oom_metric: put-metric-data FAILED (delta=${delta})" >&2
    exit 1
fi

if (( delta > 0 )); then
    # The metric says "something died"; the log says what. Without this the
    # alarm sends you to a box with no record of which unit was killed.
    echo "emit_oom_metric: ${delta} NEW oom kill(s):"
    printf '%s' "$lines" | grep "$PATTERN" | sed 's/^/  /'
else
    echo "emit_oom_metric: no new oom kills"
fi
