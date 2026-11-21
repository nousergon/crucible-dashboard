#!/bin/bash
# emit_service_memory.sh — record per-service anon+swap every run; publish
# only the TOTAL to CloudWatch, and only when it reaches the T1-8 line.
#
# WHERE THE SERIES LIVES (runbook clause SAH-5-T1-8-per-service-series, as
# amended by Brian's 2026-10-15 "Cut redundant" ruling, alpha-engine-config-
# I11792). Every run writes ONE JSON record — every budgeted unit including
# explicit zeros, the total, MemTotal and the T1-8 line — to:
#   1. stdout, i.e. the journal (`journalctl -u emit-service-memory.service`);
#   2. s3://alpha-engine-research/dashboard/box/service-memory/<UTC day>.jsonl,
#      the day's file re-uploaded whole each run (fail LOUD if that fails).
# That record is where a breach is attributed to a service. CloudWatch gets
# `ServiceMemoryTotalMiB` ONLY, on a run whose total is at or above
# `max_steady_state_fraction` x MemTotal — the line check_memory_budget.py's
# T1-8 bound uses.
#
# WHY THE PER-UNIT `ServiceMemoryMiB{Unit}` SERIES ARE NO LONGER PUBLISHED.
# The 2026-10-09 amendment (M2) published them "only on breach" to save
# ~$5.40/month. It saved nothing: the box has been in STANDING T1-8 breach
# (total 1.9-2.6 GB against a ~1.9 GB line), so all 336 of 336 hours measured
# 2026-10-01 -> 10-15 published, and the 16 per-unit series were ~$4.80 of the
# ~$7.30/month custom-metric bill. Nothing alarmed on them and no surface read
# them; they duplicated, value for value, the five-minute record above. The
# total stays in CloudWatch because it is the number the T1-8 bound is
# evaluated against and the one that can carry an alarm.
#
# WHY THIS EXISTS
# ---------------
# On 2026-08-20 the box reported `BREACH: steady-state working set 2150 MB is
# 56% of RAM`. Answering the only question that matters — WHICH service is
# using it, and is any of them GROWING — took a whole session of hand-run SSM
# snapshots, and still ended inconclusive on the growth question. Nothing on
# this box records per-service memory over time.
#
# What WAS available: `AlphaEngine/Host mem_available`, a box-level total. It
# showed availability falling ~300 MB across the fifteen hours after a
# restart, which reads like a leak — and per-service sampling twenty minutes
# apart showed no service growing at all, with the decline explained by one
# Streamlit app ballooning and releasing ~98 MB. A box-level total cannot tell
# those two apart, and the difference is "hunt a leak" versus "the box is fine".
#
# It also could not settle the sizing question it was raised for. `t3.medium`
# → `t3.large` is ~$30/month of new uncovered spend on a ~$62/month account
# (shared-application-host-policy.md §5 T1-7), and the argument for it rested
# on a number nobody could attribute. A resize bought on an unattributed
# number buys nothing if the cause is a leak, because the leak refills the
# larger box.
#
# ANON + SWAP, NOT memory.current
# -------------------------------
# `memory.current` includes reclaimable page cache and is CAPPED by
# `MemoryHigh`, so for any throttled unit it reports the cap rather than the
# demand. `litellm-proxy.service` measured 785 MB of `memory.current` against
# a 786 MB `MemoryHigh` while holding 269 MB more in swap — the reading was
# pinned to the cap and understated the service by a third.
#
# anon + swap is the quantity a cap cannot censor: pages pushed to swap by
# reclaim are still the service's demand, they are simply not resident. This
# is the same definition `check_memory_budget.py` uses for its working-set
# bound, so the series here and that check's verdict cannot disagree.
#
# The atomic box-health memory bundle supplies the same reconciler and budget
# as the admission check. Keep partial observations in the journal and S3
# with null totals. A partial total would misrepresent the CloudWatch series.
# Runs every 5 min via emit-service-memory.timer.

set -uo pipefail

NAMESPACE="AlphaEngine/Host"
REGION="us-east-1"
MEMINFO="${MEMINFO_FILE:-/proc/meminfo}"
STATE_DIR="${STATE_DIRECTORY:-/var/lib/emit-service-memory}"
S3_PREFIX="${SERVICE_MEMORY_S3_PREFIX:-s3://alpha-engine-research/dashboard/box/service-memory}"
KEEP_DAYS=3
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="${MEMORY_BUNDLE_ROOT:-/usr/local/lib/box-health-memory}"
PYTHON="${PYTHON:-python3}"

# Running directly from the checkout is the standalone diagnostic entry point.
# The installed service resolves and holds one generation until Python returns.
if [[ -r "$HERE/check_memory_budget.py" ]]; then
    CHECKER="$HERE/check_memory_budget.py"
else
    exec 9<"$BUNDLE/.lock" && flock -s 9 || exit 1
    CHECKER="$BUNDLE/current/check_memory_budget.py"
fi
args=(--sample --meminfo "$MEMINFO")
[[ -z "${BUDGET_FILE:-}" ]] || args+=(--budget "$BUDGET_FILE")
[[ -z "${MEMORY_CGROUP_ROOT:-}" ]] || args+=(--cgroup-root "$MEMORY_CGROUP_ROOT")

fetch_instance_id() {
    local token
    token=$(curl -sf --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null) || return 1
    [[ -n "$token" ]] || return 1
    curl -sf --max-time 2 -H "X-aws-ec2-metadata-token: ${token}" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null
}

# Same three-attempt shape as emit_oom_metric.sh, and for the same measured
# reason: `curl -s` without `-f` exits 0 on an IMDS 401, so a timed-out token
# PUT yields an empty id that the AWS CLI then rejects as a malformed
# dimension (observed on this box 2026-08-20, twice in four hours).
INSTANCE_ID=""
for _attempt in 1 2 3; do
    INSTANCE_ID=$(fetch_instance_id || true)
    [[ "$INSTANCE_ID" =~ ^i-[0-9a-f]+$ ]] && break
    INSTANCE_ID=""
    sleep 1
done
if [[ -z "$INSTANCE_ID" ]]; then
    echo "emit_service_memory: IMDS did not yield an instance id after 3 attempts" >&2
    exit 1
fi

if ! SAMPLE=$("$PYTHON" "$CHECKER" "${args[@]}"); then
    echo "emit_service_memory: memory reconciliation could not run" >&2
    exit 1
fi
if [[ "$CHECKER" == "$BUNDLE/current/check_memory_budget.py" ]]; then
    exec 9<&-
fi

# Render the journal/S3 record and extract its numeric metric only from the
# same validated snapshot. Keep the JSON as data, including nulls and reasons.
if ! EXTRA=$("$PYTHON" -c '
import json, sys
record = json.load(sys.stdin)
record["instance_id"] = sys.argv[1]
print(json.dumps(record, separators=(",", ":")))
total = record["total_mib"]
publish = total is not None and record["at_or_over_t1_8"] is not False
print(total if publish else "-", int(record["coverage_complete"]), record["ts"][:10])
' "$INSTANCE_ID" <<< "$SAMPLE"); then
    echo "emit_service_memory: invalid reconciliation record" >&2
    exit 1
fi
RECORD=${EXTRA%%$'\n'*}
read -r METRIC_MIB COMPLETE DAY <<< "${EXTRA##*$'\n'}"
rc=0

# The journal remains available even when the upload fails.
echo "$RECORD"
if mkdir -p "$STATE_DIR" && printf '%s\n' "$RECORD" >> "$STATE_DIR/${DAY}.jsonl"; then
    if ! aws s3 cp --region "$REGION" --only-show-errors \
            "$STATE_DIR/${DAY}.jsonl" "${S3_PREFIX}/${DAY}.jsonl"; then
        echo "emit_service_memory: S3 upload FAILED for ${S3_PREFIX}/${DAY}.jsonl" >&2
        rc=1
    fi
    find "$STATE_DIR" -maxdepth 1 -name '*.jsonl' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
else
    echo "emit_service_memory: could not append to ${STATE_DIR}/${DAY}.jsonl" >&2
    rc=1
fi

if [[ "$METRIC_MIB" != "-" ]]; then
    if ! aws cloudwatch put-metric-data --region "$REGION" --namespace "$NAMESPACE" \
        --metric-data "MetricName=ServiceMemoryTotalMiB,Value=${METRIC_MIB},Unit=Megabytes,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}]"; then
        echo "emit_service_memory: put-metric-data FAILED for ServiceMemoryTotalMiB" >&2
        rc=1
    fi
fi
if [[ "$rc" -ne 0 ]]; then
    exit "$rc"
fi
if [[ "$COMPLETE" != 1 ]]; then
    echo "emit_service_memory: incomplete sample recorded with explicit coverage findings" >&2
    exit 2
fi
