#!/bin/bash
# emit_service_memory.sh — publish per-service anon+swap to CloudWatch.
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
# ONE DIMENSION, NOT TWO
# ----------------------
# Dimensioned by Unit only, in a namespace already scoped to this box's
# metrics. Adding InstanceId would double the metric count for a single-box
# fact and make every future graph carry a constant. If a second host ever
# publishes here, add the dimension then — with a reason.
#
# EVERY UNIT, EVERY RUN, INCLUDING THE QUIET ONES
# -----------------------------------------------
# Units are read from budget.yaml's coverage manifest, not from whatever is
# currently running, and a unit that is absent publishes 0 rather than
# nothing. A service that stopped must be visibly zero: a metric that simply
# ceases is indistinguishable from a dead publisher (observability-policy
# §7.4a), and "the memory freed up" and "the collector broke" are the two
# readings we would most need to tell apart.
#
# Runs every 5 min via emit-service-memory.timer, alongside emit_oom_metric.sh.
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T0-3.

set -uo pipefail

NAMESPACE="AlphaEngine/Host"
REGION="us-east-1"
CGROUP_ROOT="/sys/fs/cgroup/system.slice"
BUDGET="${BUDGET_FILE:-/home/ec2-user/alpha-engine-dashboard/infrastructure/systemd/resource-limits/budget.yaml}"

# The unit list comes from budget.yaml so this collector and the budget check
# describe the same set by construction. A unit added to the budget without
# being added here would otherwise be silently uninstrumented — which is the
# gap this script exists to close, reintroduced one level down.
#
# Deliberately a grep rather than a YAML parse: this runs on the box with the
# system python and no guaranteed PyYAML, and the field is a flat `- unit:`
# list. If budget.yaml's shape ever changes, the fallback below fires loudly
# rather than silently publishing an empty set.
read_units() {
    [[ -r "$BUDGET" ]] || return 1
    # ONLY the `services:` block. budget.yaml also carries `timers:` (30 rows,
    # including OS plumbing like dbus and systemd-resolved) and `timer_jobs:`;
    # a flat grep over the file picks those up too, and a 5-minute gauge of
    # dbus's resident memory is noise that costs money per metric. `services:`
    # is the long-running set the working-set bound is about.
    awk '/^services:/{inblock=1; next} /^[a-z_]+:/{inblock=0} inblock' "$BUDGET" \
        | grep -oE '^[[:space:]]*-[[:space:]]*unit:[[:space:]]*[A-Za-z0-9@_.-]+\.service' \
        | sed -E 's/.*unit:[[:space:]]*//' | sort -u
}

mapfile -t UNITS < <(read_units)

if (( ${#UNITS[@]} == 0 )); then
    # FAIL, do not degrade to "whatever is in the cgroup tree". Publishing a
    # set derived from a different source would produce a series that silently
    # changes meaning — worse than a gap, because the gap is visible.
    echo "emit_service_memory: could not read any unit from ${BUDGET} --" \
         "refusing to publish a set derived from somewhere else" >&2
    exit 1
fi

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

# `anon` from memory.stat plus memory.swap.current, in MiB.
# A unit with no cgroup (stopped, or never installed) reports 0 -- see the
# header: absence must be visible as zero, never as a missing datapoint.
unit_mib() {
    local d="$CGROUP_ROOT/$1" anon=0 swap=0
    if [[ -r "$d/memory.stat" ]]; then
        anon=$(awk '/^anon /{print $2; exit}' "$d/memory.stat")
        [[ "$anon" =~ ^[0-9]+$ ]] || anon=0
    fi
    if [[ -r "$d/memory.swap.current" ]]; then
        swap=$(cat "$d/memory.swap.current")
        [[ "$swap" =~ ^[0-9]+$ ]] || swap=0
    fi
    echo $(( (anon + swap) / 1048576 ))
}

METRIC_DATA=()
TOTAL_MIB=0
for unit in "${UNITS[@]}"; do
    mib=$(unit_mib "$unit")
    TOTAL_MIB=$(( TOTAL_MIB + mib ))
    METRIC_DATA+=("MetricName=ServiceMemoryMiB,Value=${mib},Unit=Megabytes,Dimensions=[{Name=Unit,Value=${unit}}]")
done

# The sum, as its own series. It is the number check_memory_budget.py's
# working-set bound is evaluated against, so having it graphed next to its
# components is what makes a breach readable without re-deriving it.
METRIC_DATA+=("MetricName=ServiceMemoryTotalMiB,Value=${TOTAL_MIB},Unit=Megabytes,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}]")

# put-metric-data caps at 1000 metrics per call and this is ~20, but batch
# anyway so adding units later cannot silently start truncating.
rc=0
for ((i = 0; i < ${#METRIC_DATA[@]}; i += 20)); do
    if ! aws cloudwatch put-metric-data \
            --region "$REGION" \
            --namespace "$NAMESPACE" \
            --metric-data "${METRIC_DATA[@]:i:20}"
    then
        echo "emit_service_memory: put-metric-data FAILED for batch at index $i" >&2
        rc=1
    fi
done

if (( rc != 0 )); then
    exit 1
fi

echo "emit_service_memory: published ${#UNITS[@]} unit(s), total ${TOTAL_MIB} MiB"
