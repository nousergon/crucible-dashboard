#!/bin/bash
# box_health.sh — lightweight resource + service watchdog for the shared
# dashboard EC2. The box runs ~4 web services
# (3 Streamlit + mnemon on bun) plus nginx on a small instance, so the
# binding constraint is RAM, not CPU. This alerts (deduped) when memory
# runs low or an expected service/port is down. Quiet on success.
#
# Co-resident services it guards (port -> service):
#   8501 dashboard.service        (alpha-engine console)
#   8502 nous-ergon-live.service  (live.nousergon.ai)
#   8503 mnemon (bun)             (memory.nousergon.ai)
#   8505 signal.service           (signal.thecyphering.com)
#   8000 metron-api.service       (Metron FastAPI backend, internal)
# (metron-web.service / :3000 retired 2026-07-22 — portfolio.nousergon.ai deprecated,
#  301s to metron.nousergon.ai/dash at the CF edge; metron-dash-web.service on :3003
#  is Metron's sole web process.)
# (robodashboard.service / :8504 decommissioned 2026-06-10 — Metron succeeded it at
#  portfolio.nousergon.ai; robodashboard is now local-only. :8504 was reused by
#  crucible-dash.service on 2026-07-08 after the #354 deploy's port survey missed
#  that :8503 was already held by the mnemon/bun co-tenant — see config#1957,
#  crucible-dashboard#356, config#1972 — and freed again by crucible-dash's own
#  retirement after the 9-D cutover soak, config#1973.)
#
# Confirm-on-retry (2026-06-04): every check is sampled up to RETRY_ATTEMPTS
# times RETRY_DELAY apart, and only problems present in EVERY sample are
# reported. This kills the dominant false-positive class — a single-shot
# `ss -tln` on this busy box intermittently returns a TRUNCATED socket list,
# so a random subset of the five ports went missing for one probe and paged
# even though every service was provably up (root-caused from S3 dedup
# markers: 9 firings in one day, each naming a different random port subset,
# with zero corresponding service restarts). The same retry window also
# absorbs the port gap during a deploy restart. A genuinely-down
# service/port (or a real PATH/tooling regression) stays missing across all
# samples and still pages on the first run. Confirmation adds latency only on
# the non-clean path; the common all-healthy case still exits after one cheap
# sample.
#
# Window sizing (2026-06-18): the window must exceed the SLOWEST guarded
# service's cold-start, or a deploy restart false-pages. The binding constraint
# is metron-api (uvicorn), which takes ~5s from `systemctl restart` to binding
# :8000 ("Application startup complete"). The original 3x2s (~4s) window was
# tuned for Streamlit's ~2s gap and was narrower than uvicorn's cold-start, so a
# manual `systemctl restart metron-api` landing just before a probe paged on
# "port not listening: 8000" even though the service came up seconds later (one
# such false page on 2026-06-18 during a Metron deploy). 4x4s (~12s) clears it
# with margin. Cost is paid only on the non-clean path, once per 10-min tick.
#
# Alerts go through krepis.alerts (SNS alpha-engine-alerts +
# Telegram), which dedups so a persistent problem only pages once per
# window. Installed to /usr/local/bin by install-box-health.sh; scheduled
# by box-health.timer (every 10 min).
set -uo pipefail

ENV_FILE="/home/ec2-user/.alpha-engine.env"
VENV_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"
BUDGET_CHECK="/home/ec2-user/alpha-engine-dashboard/infrastructure/check_memory_budget.py"

# Load Telegram creds etc. (SNS auth comes from the instance role).
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export AWS_REGION="${AWS_REGION:-us-east-1}"
# Self-discover this box's instance id (IMDSv2) for alert context — the box
# identifies itself rather than hardcoding the id. Degrade gracefully.
_imds_tok=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
INSTANCE_ID=$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: ${_imds_tok}" http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "dashboard-ec2")

# ── thresholds ──────────────────────────────────────────────────────────
MEM_MIN_MB=150                       # alert if MemAvailable drops below this
DISK_WARN_PCT=80                     # root-disk warn band (page, deduped)
DISK_CRIT_PCT=90                     # root-disk critical band (page, deduped)
# Service/port coverage comes from the GENERATED manifest, which is rendered
# from infrastructure/systemd/resource-limits/budget.yaml — the box's single
# service registry. Do not hand-edit the arrays here.
#
# This used to be a hand-maintained list and it drifted exactly as you would
# expect: on 2026-07-27 the installed copy listed 8 services, the copy in git
# listed 5, and NEITHER covered nginx — the single ingress for all ten vhosts.
# Six of fourteen services and seven of thirteen ports were unmonitored, and
# the gap presented as GREEN, which is worse than presenting as broken.
MANIFEST="/etc/alpha-engine/box-services.conf"
if [ -r "$MANIFEST" ]; then
    # shellcheck source=/dev/null
    . "$MANIFEST"
    MANIFEST_OK=1
else
    # Fall back so a missing manifest degrades to partial monitoring rather
    # than NO monitoring — but say so loudly, because partial coverage that
    # looks total is the exact failure this replaced.
    MANIFEST_OK=0
    SERVICES=(dashboard.service nous-ergon-live.service signal.service \
              metron-api.service metron-dash-web.service crucible-dash-api.service \
              crucible-dash-web.service litellm-proxy.service llm-egress-proxy.service \
              telos-web.service vires.service mnemon.service nousergon-auth.service \
              nginx.service)
    PORTS=(8501 8502 8503 8505 8000 3003 8506 3002 3001 8530 4100 8980 8990 443)
    EXPECTED_SERVICE_COUNT=14
fi
# Minimum MemoryHigh events in one 10-min tick before throttling is reported.
#
# Not zero. A handful of reclaim events during a deploy restart or a one-off
# request spike is normal and self-corrects; paging on that would recreate the
# noise problem in a new form.
#
# Sized from measurement, not intuition. metron-api's counter stood at 7347 on
# 2026-07-28 — which looks like continuous throttling and is not: sampled every
# 20s for 2 minutes it did not move once, and memory.pressure read 0.00 at
# avg10/avg60/avg300 for both `some` and `full`. All 7347 events were a startup
# burst during worker warm-up (cgroup created 17:24:24 UTC) that ended once the
# service settled. The at-rest rate is ZERO, so 10 events inside a single tick
# is unambiguously a burst rather than background noise.
#
# A restart therefore CAN trip this, and that is intended: a large delta after
# a restart means the cap is still below the service's startup peak — the exact
# condition this alert should surface, and the one being corrected for
# metron-api in budget.yaml (config-I5216). The complementary memory.pressure
# check above fires independently on sustained stall, so a service suffering
# continuously is caught by either.
CGROUP_HIGH_DELTA_MIN=10

# Where the previous run's throttle counters live. /var/lib, not /tmp: a
# tmpfiles cleanup mid-window would silently re-baseline and hide throttling.
#
# $STATE_DIRECTORY is exported by systemd from StateDirectory= in the unit,
# which is what makes the directory exist AND be writable by User=ec2-user.
# The literal fallback is for running the script by hand outside systemd.
THROTTLE_STATE_DIR="${STATE_DIRECTORY:-/var/lib/box-health}"
THROTTLE_STATE="${THROTTLE_STATE_DIR}/cgroup-high-counts"

RETRY_ATTEMPTS=4                     # samples before a problem is confirmed
RETRY_DELAY=4                        # seconds between confirmation samples (4x4s ~12s window > metron-api ~5s cold-start)

# Resolve `ss` by absolute path once: it lives in /usr/sbin, which the systemd
# unit's PATH does not include, so a bare `ss` is "command not found" under the
# service. The script also sets PATH in the unit, so this is defense in depth.
SS_BIN=""
for cand in /usr/sbin/ss /sbin/ss /usr/bin/ss /bin/ss; do
    [ -x "$cand" ] && { SS_BIN="$cand"; break; }
done

# root_disk_pct — used percent of / as a bare integer (empty on probe failure).
root_disk_pct() {
    df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9'
}

# Publish resource gauges to CloudWatch (AlphaEngine/Box) on EVERY tick, healthy
# or not — the paired CW alarm treats MISSING data as breaching, so a box too
# broken to publish (disk full, agent dead, instance stopped) still pages. This
# is the independent channel for the 2026-07-11 class where disk-full killed
# SSM while the instance pinged Online (config#2227).
emit_metrics() {
    local disk_pct mem_avail_mb
    disk_pct=$(root_disk_pct)
    mem_avail_mb=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo)
    # Swallowed failure mode: transient CW/credential error on a metrics-only
    # publish. The health checks below must still run; the recording surface is
    # the journal line here PLUS the alarm's missing-data breach if it persists.
    aws cloudwatch put-metric-data --namespace "AlphaEngine/Box" \
        --metric-data \
        "MetricName=disk_used_percent,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}],Value=${disk_pct:-0},Unit=Percent" \
        "MetricName=mem_available_mb,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}],Value=${mem_avail_mb:-0},Unit=Megabytes" \
        2>&1 | head -1 | sed 's/^/box_health: metric publish failed: /' >&2 || true
}

# classify_identity — decide whether ONE unit's User=/Group= makes it
# unstartable. Echoes a problem line if the account cannot be resolved; echoes
# nothing when healthy.
#
# Pure, for the same reason classify_timer is: the interesting case is the one
# that must NOT fire. An unset User= is the overwhelmingly common case (the
# unit inherits root) and is perfectly healthy — a version of this check that
# treated empty as unresolvable would flag most units on the box, get tuned
# down, and end up excluding the class it exists to catch. That is precisely
# the failure mode this check was born from (nous-ergon-ops-I155, the 15th
# instance of guard-filter-excludes-the-class-it-protects), so the empty case
# is asserted explicitly in test_box_health_unit_identity.sh rather than left
# to inspection.
#
#   $1 unit name  $2 field (User|Group)  $3 configured value  $4 resolves (yes|no)
classify_identity() {
    local unit="$1" field="$2" value="$3" resolves="$4"
    [ -z "$value" ] && return 0                 # unset — inherits root; healthy
    [ "$resolves" = "yes" ] && return 0
    echo "unit cannot restart: $unit has $field=$value, which does not resolve"
}

# classify_timer — decide whether ONE enabled timer is dead, from its systemd
# properties alone. Echoes a problem line if it will not fire again; echoes
# nothing when healthy.
#
# A pure function of its arguments (no systemd calls) specifically so the
# classification can be unit-tested against synthetic property blocks — see
# test_box_health_timer_deadman.sh. The predicate this replaced was never
# tested, and shipped a defect that fired on 100% of runs (below).
#
#   $1 unit name  $2 ActiveState  $3 SubState
#   $4 NextElapseUSecRealtime  $5 NextElapseUSecMonotonic
classify_timer() {
    local name="$1" active="$2" sub="$3" next_real="$4" next_mono="$5"

    # An `enabled` timer that is not `active` will not fire again until a
    # reboot or a manual `systemctl start`. This is the EXACT signature of the
    # outage this check exists for: metron-refresh.timer sat `enabled` +
    # `inactive` for two days after its 2026-07-25 OOM kill (config-I4487).
    # Named distinctly from the never-fire-again case below because the remedy
    # differs and is mechanical: `systemctl start <timer>`.
    if [ "$active" != "active" ]; then
        echo "timer enabled but not active (will not fire until reboot): $name"
        return
    fi

    # SubState=running means the timer's OWN triggered service is executing
    # right now. systemd deliberately does not compute a next-elapse while a
    # timer is in that state (NextElapseUSecMonotonic=infinity), and
    # `systemctl list-timers` renders that as `-` in the NEXT column — visually
    # identical to a dead timer.
    #
    # This is why the previous `NEXT == "-"` table heuristic was wrong. It read
    # in-flight as dead, and box_health.sh runs INSIDE box-health.service, so
    # box-health.timer was guaranteed to be mid-trigger at every single sample:
    # 144 of 144 runs over 36h flagged the watchdog's own timer, paging hourly
    # (the dedup window) about a timer that was provably firing on schedule.
    # The same race hits any other timer whose job outlives the confirmation
    # window — so this is a general defect, not a box-health special case, and
    # it is fixed by reading state rather than by excluding one unit.
    if [ "$sub" = "running" ]; then
        return
    fi

    # "No next elapse" is spelled differently per timer kind, and BOTH
    # properties are always present, so neither alone is a sufficient test:
    #   calendar timer  -> Realtime=<timestamp>   Monotonic=0
    #   monotonic timer -> Realtime=(empty)       Monotonic=<timespan>
    #   no next elapse  -> Realtime=(empty)       Monotonic=0 | infinity
    case "$next_mono" in ""|0|infinity) next_mono="" ;; esac
    if [ -z "$next_real" ] && [ -z "$next_mono" ]; then
        echo "timer will never fire again: $name"
    fi
}

# human_age — seconds as a compact age ("45m", "31h", "9d") for alert text.
# A raw second count is unreadable at a glance and the alert is read by a human
# on a phone, not parsed.
#
# Days only past 48h, deliberately. Integer division at a 24h cutoff renders
# BOTH 26h and 31h as "1d", so a genuinely breached alert reads
# "has not run in 1d (budget 1d)" — self-contradictory, and the natural
# reading is that nothing is wrong. Most budgets on this box sit in the
# 26-30h band precisely because they are daily jobs with slack, so that
# collision is the common case, not an edge case. Caught by its own test.
human_age() {
    local s="$1"
    if   [ "$s" -ge 172800 ]; then echo "$((s / 86400))d"
    elif [ "$s" -ge 3600 ];   then echo "$((s / 3600))h"
    elif [ "$s" -ge 60 ];     then echo "$((s / 60))m"
    else echo "${s}s"
    fi
}

# classify_timer_staleness — decide whether ONE timer's JOB is healthy, as
# opposed to whether its SCHEDULE is (that is classify_timer above).
#
# These are genuinely different questions and the box needs both. A timer whose
# service exits non-zero on every single fire — or one whose OnCalendar was
# mis-edited to a far-future date — is `active`, `waiting`, with a perfectly
# valid next elapse. classify_timer calls it healthy, correctly, because the
# scheduler IS healthy. Only execution outcome exposes it. metron-refresh's
# 2026-07-25 OOM kill was caught by the scheduler check purely by luck: it
# happened to also stop the timer (config-I4487, config-I5209).
#
# Pure function of its arguments, same as classify_timer, so the thresholds and
# the edge cases below are unit-testable without systemd.
#
#   $1 unit name          $2 now (epoch seconds)
#   $3 last trigger (epoch seconds, EMPTY if never triggered)
#   $4 max staleness (seconds, EMPTY if no budget.yaml row)
#   $5 triggered service's Result
classify_timer_staleness() {
    local name="$1" now="$2" last="$3" budget="$4" result="$5" age

    # No declared budget = unmonitored. NAMED, not skipped: an enabled timer
    # with no `timers:` row in budget.yaml is a coverage hole, and a coverage
    # hole that stays quiet is how this whole class of gap presents as green.
    if [ -z "$budget" ]; then
        echo "watchdog: timer has no dead-man threshold: $name — add a timers: row to budget.yaml"
        return
    fi

    # Execution outcome. Independent of staleness: a job can fail promptly and
    # on schedule forever, which is stale by no measure and broken by any.
    if [ -n "$result" ] && [ "$result" != "success" ]; then
        echo "timer job failing: $name (last run result=$result)"
    fi

    # Never triggered. Not reportable as stale — there is no baseline to
    # measure from, and a freshly-installed timer is legitimately in this
    # state. Whether it will EVER fire is classify_timer's question, and it
    # answers it. reboot-if-needed.timer sits here in normal operation.
    [ -n "$last" ] || return

    # A non-numeric last-trigger reaching the arithmetic below is fatal under
    # `set -u`: bash evaluates bare words inside $(( )) as variable names, so
    # "Tue 2026-07-28 ..." aborts the ENTIRE snapshot with "Tue: unbound
    # variable" — taking every other check on the box down with it. That is not
    # hypothetical: `systemctl show --timestamp=unix` silently does NOT apply
    # to LastTriggerUSec (verified on systemd 252, 2026-07-28), so the first
    # live run crashed exactly this way. Reported, never evaluated.
    case "$last" in
        ''|*[!0-9]*)
            echo "watchdog: cannot parse timer last-trigger ($last): $name"
            return ;;
    esac

    age=$((now - last))
    # A last-trigger in the future means a clock step, not a healthy timer.
    # Reported rather than silently passed by the `>` comparison below.
    if [ "$age" -lt 0 ]; then
        echo "watchdog: timer last-trigger is in the future (clock skew?): $name"
        return
    fi
    if [ "$age" -gt "$budget" ]; then
        echo "timer has not run in $(human_age "$age") (budget $(human_age "$budget")): $name"
    fi
}

# classify_throttle_delta — decide whether a cgroup's MemoryHigh throttling is
# ACTIVE, from the counter's movement rather than its total.
#
# memory.events::high is MONOTONIC for the life of the cgroup: it only resets
# when the cgroup is recreated (a service restart or a reboot). Alerting on
# `> 0` therefore has two defects that make it useless as a signal:
#
#   1. One transient spike at 03:00 pages forever. The condition can never
#      clear on its own.
#   2. FIXING THE CAP DOES NOT CLEAR IT. The alert survives the remedy, so it
#      cannot be used to confirm that the remedy worked — which is precisely
#      what config-I5216's acceptance criteria ask it to do.
#
# It also cannot distinguish "throttled 7000 times in the last hour" from
# "throttled once three weeks ago". Only the delta between samples can.
#
# Pure function of its arguments so the threshold behaviour is unit-testable.
#
#   $1 unit name  $2 current counter  $3 baseline counter (EMPTY on first run)
#   $4 minimum delta to report
# throttle_baseline UNIT — the counter recorded at the end of the previous RUN,
# or empty if there is none. Empty is a valid, expected state (first run after
# a deploy or reboot) and classify_throttle_delta treats it as "no comparison".
throttle_baseline() {
    [ -r "$THROTTLE_STATE" ] || return 0
    awk -v u="$1" '$1==u {print $2; found=1} END{if(!found) print ""}' \
        "$THROTTLE_STATE" 2>/dev/null
}

# throttle_baseline_write — snapshot every monitored unit's counter. Called
# ONCE per run, after alerting, never between confirmation samples (see the
# call site for why that ordering is load-bearing).
throttle_baseline_write() {
    local s evt c tmp
    mkdir -p "$THROTTLE_STATE_DIR" 2>/dev/null || return 0
    tmp="${THROTTLE_STATE}.$$"
    : > "$tmp" || return 0
    for s in "${SERVICES[@]}"; do
        evt="/sys/fs/cgroup/system.slice/${s}/memory.events"
        [ -r "$evt" ] || continue
        c=$(awk '/^high/{print $2}' "$evt" 2>/dev/null)
        case "$c" in ''|*[!0-9]*) continue ;; esac
        printf '%s %s\n' "$s" "$c" >> "$tmp"
    done
    # Atomic swap: a half-written state file would produce garbage baselines on
    # the next run, and a garbage baseline reads as a huge delta.
    mv -f "$tmp" "$THROTTLE_STATE" 2>/dev/null || rm -f "$tmp"
}

classify_throttle_delta() {
    local name="$1" current="$2" baseline="$3" floor="$4" delta

    case "$current" in ''|*[!0-9]*)
        echo "watchdog: cannot read cgroup throttle counter for $name"
        return ;;
    esac

    # No baseline yet — first run after a deploy or reboot. Nothing to compare
    # against, and reporting the lifetime total here would reintroduce exactly
    # the defect this replaces. Silent by design; the next sample has a
    # baseline. Sustained throttling is still caught 10 minutes later.
    case "$baseline" in ''|*[!0-9]*) return ;; esac

    delta=$((current - baseline))
    # A counter that went BACKWARDS means the cgroup was recreated (service
    # restarted) between samples. Not an error and not throttling — the next
    # sample re-baselines.
    [ "$delta" -lt 0 ] && return

    if [ "$delta" -ge "$floor" ]; then
        echo "cgroup throttle: $name hit MemoryHigh ${delta}x since the last check"
    fi
}

# publish_verdict COUNT — put the watchdog's OWN conclusion on the metrics path.
#
# Why this exists (config-I5211): until now the verdict travelled ONLY via
# `krepis.alerts publish` (SNS + Telegram). Metrics and alerts are separate code
# paths with separate failure modes, so a broken alert path means a confirmed
# problem is found and then silently dropped — while emit_metrics keeps
# publishing happily, so the box-liveness alarm stays green. The box looks
# healthy precisely because the part that says otherwise is the part that broke.
# That is not hypothetical on this fleet: `python -m nousergon_lib.alerts` was a
# guard-less silent exit-0 no-op on lib 0.81.0 (config#1646).
#
# A count, deliberately, not the problem text. WHICH problem and WHY is the
# on-box alert's job and it does it well; duplicating that detail into CloudWatch
# would make both layers wrong in the same way when the source is wrong. This
# layer answers one question the other cannot: "is the watchdog finding
# anything, regardless of whether it can tell me about it?"
publish_verdict() {
    aws cloudwatch put-metric-data --namespace "AlphaEngine/Box" \
        --metric-data \
        "MetricName=health_problems,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}],Value=${1:-0},Unit=Count" \
        2>&1 | head -1 | sed 's/^/box_health: verdict publish failed: /' >&2 || true
}

# snapshot_problems — run the full check ONCE, printing one problem per line.
# No shared state; the caller samples it repeatedly and keeps the intersection.
snapshot_problems() {
    # memory headroom
    local mem_avail_mb
    mem_avail_mb=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo)
    if [ "${mem_avail_mb:-0}" -lt "$MEM_MIN_MB" ]; then
        echo "low memory: <${MEM_MIN_MB}MB available"
    fi

    # root-disk headroom. Problem strings are STATIC (no live percent) because
    # the confirm-on-retry intersection matches lines exactly — a fluctuating
    # number would never confirm. Exact percent goes to the journal via the
    # emit_metrics tick and the confirmed-problems log line.
    local disk_pct
    disk_pct=$(root_disk_pct)
    if [ -n "$disk_pct" ]; then
        if [ "$disk_pct" -ge "$DISK_CRIT_PCT" ]; then
            echo "disk critical: root >=${DISK_CRIT_PCT}% used"
        elif [ "$disk_pct" -ge "$DISK_WARN_PCT" ]; then
            echo "disk high: root >=${DISK_WARN_PCT}% used"
        fi
    else
        # Fail loud: a broken df probe is a watchdog malfunction, same class as
        # the ss guard below — report distinctly, never silently skip the check.
        echo "watchdog: df probe failed (cannot verify root disk)"
    fi

    # systemd services
    local s
    for s in "${SERVICES[@]}"; do
        systemctl is-active --quiet "$s" || echo "service down: $s"
    done

    # Unit identity resolvability.
    #
    # A unit whose User= (or Group=) does not resolve cannot start: systemd
    # fails it at step USER with 217/USER, before ExecStart. The critical
    # property is that this is INVISIBLE to every check above — a service
    # already running when its User= became unresolvable stays `active` and
    # reports healthy right up until something restarts it.
    #
    # That is not hypothetical. On 2026-07-28 thirteen units were given
    # User=svc-<name> for accounts nothing had created (nous-ergon-ops-I155).
    # The five the deploy restarted died immediately; the other eight kept
    # reporting active and would have died on the next restart — which
    # reboot-if-needed.timer makes an unattended, all-at-once event. Nothing
    # here could see the difference between those eight and genuinely healthy
    # services, because "is it running" and "could it start again" are
    # different questions and only the first was ever asked.
    #
    # Cheap enough to be unconditional: one getent per declared service.
    local u g
    for s in "${SERVICES[@]}"; do
        u=$(systemctl show "$s" -p User --value 2>/dev/null)
        classify_identity "$s" User "$u" \
            "$(getent passwd "$u" >/dev/null 2>&1 && echo yes || echo no)"
        g=$(systemctl show "$s" -p Group --value 2>/dev/null)
        classify_identity "$s" Group "$g" \
            "$(getent group "$g" >/dev/null 2>&1 && echo yes || echo no)"
    done

    # Box memory budget: what systemd ACTUALLY loaded vs what budget.yaml
    # declares, plus the observation-quality checks (censored / stale / orphan).
    #
    # WHY THIS IS HERE AT ALL
    # check_memory_budget.py's own docstring says --installed "is the on-box
    # mode, run by box_health.sh". It was not. Verified 2026-07-28: nothing on
    # the box or in CI invoked --installed -- only --declared, from the
    # installer, which checks budget.yaml against ITSELF and can never see the
    # box. So every --installed check (cap drift, uncapped service, and now
    # censored/stale observations and orphan drop-ins) was written, tested, and
    # never executed. A computed signal with no subscriber is not monitoring,
    # and the docstring asserting the integration made it look like one.
    #
    # STATIC problem string, detail to the journal. This is load-bearing: the
    # confirm-on-retry intersection matches lines EXACTLY, and this check's
    # messages carry live byte counts that move between samples ("holds 185 MB
    # (1.7x)"). Emitting them verbatim would produce a problem that can never
    # confirm and therefore never alerts -- a guard that looks wired and is not,
    # which is the same defect this block exists to correct.
    if [ -r "$BUDGET_CHECK" ]; then
        local budget_out
        if ! budget_out=$("$VENV_PY" "$BUDGET_CHECK" --installed --quiet 2>&1); then
            echo "memory budget: cap drift or unreliable observation (detail in journal)"
            printf 'box_health: memory budget detail:\n%s\n' "$budget_out" >&2
        fi
    else
        # Same class as the df probe below: a check that cannot run is a
        # watchdog malfunction, reported distinctly rather than skipped.
        echo "watchdog: memory budget check missing ($BUDGET_CHECK)"
    fi

    # Manifest presence. Reported as a problem in its own right: running on the
    # fallback list means coverage is frozen at whatever was hardcoded here,
    # which is the drift this mechanism exists to end.
    if [ "${MANIFEST_OK:-0}" -ne 1 ]; then
        echo "watchdog: service manifest missing ($MANIFEST) — using stale fallback list"
    fi

    # Coverage self-check. Any enabled, non-oneshot unit that is neither
    # monitored nor explicitly excluded is NAMED here. This is the guard that
    # stops the list falling behind again: a service added to the box but not
    # to budget.yaml surfaces as an alert instead of going quietly unmonitored.
    # Without it, the failure mode is a green watchdog.
    #
    # Named, not counted. A count says "something is missing" without saying
    # what, and cannot distinguish a new app service from OS plumbing — a
    # count-based version of this check false-alarmed on dbus aliases and the
    # CloudWatch agent when first deployed 2026-07-27.
    local u n
    local unmonitored=""   # see the note above: `local u n unmonitored` leaves it UNSET
    for u in /etc/systemd/system/*.service; do
        [ -e "$u" ] || continue
        n=$(basename "$u")
        case " ${SERVICES[*]} ${MONITOR_EXCLUDE[*]:-} " in *" $n "*) continue ;; esac
        systemctl is-enabled --quiet "$n" 2>/dev/null || continue
        [ "$(systemctl show "$n" -p Type --value 2>/dev/null)" = "oneshot" ] && continue
        unmonitored="${unmonitored}${n} "
    done
    if [ -n "${unmonitored:-}" ]; then
        echo "watchdog: unmonitored enabled service(s): ${unmonitored%% } — add to budget.yaml or manifest_exclude"
    fi

    # Timer dead-man. A timer-driven oneshot is correctly `inactive` almost all
    # of the time, so the service/port checks above structurally CANNOT see it
    # die — which is why metron-refresh sat dead for two days after its
    # 2026-07-25 OOM kill with nothing noticing (alpha-engine-config-I4487).
    #
    # Enumerated from unit FILES, not from `systemctl list-timers`, and judged
    # on `systemctl show` properties, not on that table's columns. The table is
    # a human-facing format whose column count varies with timer kind and whose
    # NEXT field is ambiguous (see classify_timer); reading properties is both
    # unambiguous and version-stable. Column $1 of list-unit-files is always
    # the unit name, so enumeration carries no positional fragility.
    #
    # TWO checks run per timer, answering different questions:
    #   classify_timer            — is it scheduled to fire?      (scheduler)
    #   classify_timer_staleness  — did it run, and did it work?  (execution)
    # A job that fires on time and fails every run is invisible to the first
    # and caught only by the second (config-I5209).
    local t props active sub next_real next_mono timer_units _k _v
    local svc last_epoch now_epoch result budget staleness_ok
    now_epoch=$(date +%s)

    # The thresholds live in the generated manifest, so they can be absent for
    # two different reasons and the difference matters. An empty/undeclared map
    # means the manifest predates the thresholds (an installer that did not
    # re-run) — ONE line saying so. Reporting all ~22 timers as individually
    # unmonitored in that state buries the single actionable cause under its
    # own symptoms.
    staleness_ok=1
    if ! declare -p TIMER_MAX_STALENESS >/dev/null 2>&1 \
       || [ "${#TIMER_MAX_STALENESS[@]}" -eq 0 ]; then
        staleness_ok=0
        [ "${MANIFEST_OK:-0}" -eq 1 ] && echo "watchdog: timer dead-man thresholds absent from manifest — re-run install-box-health.sh"
    fi
    timer_units=$(systemctl list-unit-files --type=timer --no-legend 2>/dev/null | awk '{print $1}')
    if [ -z "$timer_units" ]; then
        # Fail loud: an empty enumeration is a watchdog malfunction, not a box
        # with no timers. Silently checking nothing is how this whole class of
        # gap presents as green.
        echo "watchdog: timer enumeration returned no units (cannot verify timers)"
    fi
    for t in $timer_units; do
        # Bare TEMPLATE units (`foo@.timer`) are not schedulable — only their
        # instances are, and an enabled instance gets its own `foo@bar.timer`
        # row in list-unit-files, so skipping the template loses no coverage.
        # `systemctl show` on a bare template returns nothing, which would
        # otherwise trip the unreadable-state guard below on every run:
        # refresh-policy-routes@.timer did exactly that in pre-merge testing.
        case "$t" in *@.timer) continue ;; esac
        # A timer that is not enabled is deliberately parked, not broken.
        systemctl is-enabled --quiet "$t" 2>/dev/null || continue
        # One show call carries every property both checks need.
        #
        # `--timestamp=unix` is NOT used here: it does not apply to
        # LastTriggerUSec (verified on systemd 252 — the property still renders
        # "Tue 2026-07-28 17:03:35 UTC"), so relying on it silently fed a
        # timestamp string into integer arithmetic. Converted explicitly below.
        props=$(systemctl show "$t" -p ActiveState -p SubState \
                    -p NextElapseUSecRealtime -p NextElapseUSecMonotonic \
                    -p LastTriggerUSec -p Unit 2>/dev/null)
        if [ -z "$props" ]; then
            echo "watchdog: cannot read timer state: $t"
            continue
        fi
        # Key-matched, not positional: `systemctl show` makes no ordering
        # guarantee, and a missing key must stay EMPTY rather than silently
        # inheriting the neighbouring property's value.
        active=""; sub=""; next_real=""; next_mono=""; last_raw=""; svc=""
        while IFS='=' read -r _k _v; do
            case "$_k" in
                ActiveState)             active="$_v" ;;
                SubState)                sub="$_v" ;;
                NextElapseUSecRealtime)  next_real="$_v" ;;
                NextElapseUSecMonotonic) next_mono="$_v" ;;
                Unit)                    svc="$_v" ;;
                LastTriggerUSec)         last_raw="$_v" ;;
            esac
        done <<< "$props"
        # A blank ActiveState means the parse failed, NOT that the timer is
        # stopped — report it as a watchdog malfunction instead of paging a
        # false dead-timer (no-silent-fails, and no silent false alarms).
        if [ -z "$active" ]; then
            echo "watchdog: cannot parse timer state: $t"
            continue
        fi
        classify_timer "$t" "$active" "$sub" "$next_real" "$next_mono"

        # Execution-outcome half. Skipped wholesale when the threshold map is
        # unavailable — that condition is already reported once, above.
        [ "$staleness_ok" -eq 1 ] || continue
        budget="${TIMER_MAX_STALENESS[$t]:-}"
        # Result of the unit this timer triggers, not of the timer itself.
        result=""
        [ -n "$svc" ] && result=$(systemctl show "$svc" -p Result --value 2>/dev/null)
        # Convert systemd's human timestamp to epoch. Guarded on non-empty
        # because `date -d ""` returns TODAY'S MIDNIGHT and exits 0 — a
        # never-triggered timer would otherwise acquire a plausible, wrong
        # last-run time and be judged against it. On a parse failure the raw
        # value is passed through deliberately: classify_timer_staleness
        # reports it verbatim rather than evaluating it.
        last_epoch=""
        if [ -n "$last_raw" ]; then
            last_epoch=$(date -d "$last_raw" +%s 2>/dev/null) || last_epoch="$last_raw"
            [ -n "$last_epoch" ] || last_epoch="$last_raw"
        fi
        classify_timer_staleness "$t" "$now_epoch" "$last_epoch" "$budget" "$result"
    done

    # ── per-service cgroup memory pressure (alpha-engine-config-I4512) ─────
    # The 2026-07-27 failure: two services (litellm-proxy, llm-egress-proxy)
    # were pinned at their MemoryHigh ceiling with memory.pressure ~60% and
    # memory.events high in the thousands, yet nothing surfaced this until
    # they failed to restart. memory.pressure some avg10 > 10 means the
    # service is spending >10% of time stalled on reclaim — a sustained
    # throttle. memory.events high > 0 means the cgroup has hit its soft
    # limit since boot.
    local cg evt pressure high_count throttle_state_seen=0
    for s in "${SERVICES[@]}"; do
        # cgroup v2 uses literal unit names under system.slice/ — hyphens and
        # dots are NOT hex-escaped for service units (confirmed on the actual
        # box 2026-07-28).  Hex escaping (e.g. \x2d) is only for slice unit
        # names where the prefixed "system-" separator must be distinguishable
        # from a hyphen in the unit's own name.
        cg="/sys/fs/cgroup/system.slice/${s}/memory.pressure"
        if [ -r "$cg" ]; then
            pressure=$(awk '/^some avg10/{val=$2+0; if(val>10) print val}' "$cg" 2>/dev/null)
            if [ -n "$pressure" ]; then
                echo "memory pressure: $s (avg10 some ${pressure}%)"
            fi
        fi
        evt="/sys/fs/cgroup/system.slice/${s}/memory.events"
        if [ -r "$evt" ]; then
            high_count=$(awk '/^high/{print $2}' "$evt" 2>/dev/null)
            # Baseline is read from the file written at the END of the previous
            # RUN, never updated mid-run. That is load-bearing: snapshot_problems
            # is sampled up to RETRY_ATTEMPTS times and only problems present in
            # EVERY sample are reported. A baseline refreshed per sample would
            # make samples 2..N see a delta of ~0, the intersection would drop
            # the line, and real throttling would be silently filtered out by
            # the very mechanism meant to suppress false positives.
            classify_throttle_delta "$s" "$high_count" \
                "$(throttle_baseline "$s")" "$CGROUP_HIGH_DELTA_MIN"
            throttle_state_seen=1
        fi
    done
    # An unwritable state directory makes the throttle check permanently
    # silent, because "no baseline" is its HEALTHY case — absence of a signal
    # reading as health is the exact class this whole check exists to end.
    # Report it rather than inheriting the silence.
    #
    # This is not hypothetical: box-health.service runs as User=ec2-user, which
    # cannot mkdir under root-owned /var/lib, so the first shipped version wrote
    # no baseline at all and the check was dead on arrival (fixed by
    # StateDirectory= in the unit).
    if [ "$throttle_state_seen" -eq 1 ]; then
        if ! mkdir -p "$THROTTLE_STATE_DIR" 2>/dev/null || [ ! -w "$THROTTLE_STATE_DIR" ]; then
            echo "watchdog: throttle state dir not writable ($THROTTLE_STATE_DIR) — cgroup throttling is UNMONITORED"
        fi
    fi
    unset cg evt pressure high_count throttle_state_seen

    # Durable-state coverage (T1-4, config-I5250). Any on-disk database that
    # budget.yaml::state[] does not declare is NAMED here.
    #
    # The point is not that undeclared state is unbacked — it is that nobody
    # has DECIDED. T1-4 accepts "replicated" or "accepted-loss with a stated
    # RPO"; what it forbids is state whose disposition was never considered,
    # because on disk that is indistinguishable from state someone chose to
    # risk. Before the 2026-07-28 audit, every database on this box was in that
    # category, including the shared identity service's.
    #
    # Cheap by construction: pruned find over one tree, and skipped entirely
    # when the manifest is absent (that condition is already reported above).
    if [ "${MANIFEST_OK:-0}" -eq 1 ] && [ "${#STATE_DECLARED[@]}" -gt 0 ]; then
        # Initialised, not merely declared: `local x` leaves x UNSET, so the
        # first `${x}` append aborts the whole snapshot under `set -u`.
        local f d matched
        local undeclared_state=""
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            matched=0
            for d in "${STATE_DECLARED[@]}"; do
                # Glob-aware: entries may be patterns, and a trailing / means
                # "anything under this directory".
                case "$f" in
                    $d|$d*) matched=1; break ;;
                esac
            done
            [ "$matched" -eq 1 ] || undeclared_state="${undeclared_state}${f} "
        done < <(find /home/ec2-user -maxdepth 4 -type f \
                     \( -name '*.db' -o -name '*.sqlite' \) \
                     -not -path '*/.cache/*' -not -path '*/node_modules/*' \
                     -not -path '*/.venv/*' -not -path '*/.git/*' 2>/dev/null)
        if [ -n "${undeclared_state:-}" ]; then
            echo "watchdog: undeclared durable state: ${undeclared_state%% } — add a state: row to budget.yaml (T1-4)"
        fi
    fi

    # listening ports (mnemon/bun has no systemd unit here, so port is the probe).
    if [ -z "$SS_BIN" ]; then
        # Fail loud: a missing probe tool is a watchdog malfunction, NOT a port
        # outage. Reporting it distinctly stops a tooling/PATH regression from
        # masquerading as a fake all-ports-down alert (no-silent-fails). Persists
        # across samples, so it confirms and pages.
        echo "watchdog: ss probe unavailable (ss not found in /usr/sbin /sbin /usr/bin /bin)"
        return
    fi
    # Match ANY bind address: Streamlit binds 127.0.0.1:850x, but mnemon (bun)
    # binds *:8503, so an address-specific pattern false-alarms on 8503.
    local listening p
    listening=$("$SS_BIN" -tln 2>/dev/null)
    if [ -z "$listening" ]; then
        # Empty output from a present binary = probe failure, not 5 dead ports.
        # A transient empty read drops out on the next sample; a persistent one
        # confirms and pages.
        echo "watchdog: ss probe returned no output (cannot verify ports)"
        return
    fi
    for p in "${PORTS[@]}"; do
        echo "$listening" | grep -qE ":$p\b" || echo "port not listening: $p"
    done
}

# Gauges flow on every tick regardless of health outcome (see emit_metrics).
emit_metrics

# Re-baseline the throttle counters on the way out — once per RUN, after every
# confirmation sample has read the OLD baseline. An EXIT trap rather than a
# call at the bottom because the all-healthy path `exit 0`s early; without the
# trap the baseline would only advance on runs that found a problem, so a
# healthy box would accumulate an ever-growing delta and eventually page for no
# reason.
trap throttle_baseline_write EXIT

# Confirm-on-retry: keep only problems present in EVERY sample. The common
# all-healthy path takes a single sample and exits without added latency.
confirmed=$(snapshot_problems)
if [ -z "$confirmed" ]; then
    publish_verdict 0
    exit 0
fi
attempt=1
while [ "$attempt" -lt "$RETRY_ATTEMPTS" ] && [ -n "$confirmed" ]; do
    sleep "$RETRY_DELAY"
    next=$(snapshot_problems)
    # intersection: lines present in BOTH the running set and this fresh sample
    confirmed=$(comm -12 <(printf '%s\n' "$confirmed" | sort) <(printf '%s\n' "$next" | sort))
    attempt=$((attempt + 1))
done

# all flagged problems self-healed within the confirmation window → no page
if [ -z "$confirmed" ]; then
    publish_verdict 0
    exit 0
fi

# Log the confirmed set so a firing is diagnosable from the journal directly
# (no S3 dedup-marker archaeology needed).
printf 'box_health: confirmed problems after %d samples:\n%s\n' "$attempt" "$confirmed" >&2

# Publish the verdict BEFORE alerting, so a broken alert path cannot also
# prevent the count being recorded — that ordering is the whole point.
publish_verdict "$(printf '%s\n' "$confirmed" | grep -c .)"

# build message + a dedup key derived from the problem set, so the same
# ongoing issue alerts once per dedup window rather than every 10 min.
mapfile -t problems <<< "$confirmed"
msg="dashboard EC2 (${INSTANCE_ID}) health alert:"
for p in "${problems[@]}"; do msg="$msg"$'\n'" - $p"; done
dkey="boxhealth-$(printf '%s' "${problems[*]}" | tr ' /' '__' | cut -c1-72)"

# krepis.alerts is the canonical CLI (config#1649): nousergon_lib.alerts is a
# re-export shim since lib v0.66.0 — guard-less under `python -m` on 0.81.0
# (silent exit-0 no-op, the config#1646 class). Invoke the real module.
"$VENV_PY" -m krepis.alerts publish \
    --message "$msg" \
    --severity warning \
    --source box-health \
    --dedup-key "$dkey" \
    --dedup-window-min 60 \
    || echo "box_health: alert publish failed" >&2
