#!/bin/bash
# box_health.sh — lightweight resource + service watchdog for the shared
# dashboard EC2. The box runs ~4 web services
# (3 Streamlit + mnemon on bun) plus nginx on a small instance, so the
# binding constraint is RAM, not CPU. This alerts (deduped) when memory
# runs low or an expected service/port is down. Quiet on success.
#
# Co-resident services it guards (port -> service):
#   8501 dashboard.service        (dashboard.nousergon.ai Streamlit)
#   5180 nousergon-console.service (console.nousergon.ai v2)
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
VENV_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"
BUDGET_CHECK="/home/ec2-user/alpha-engine-dashboard/infrastructure/check_memory_budget.py"
# The info tier's console emitter. Sibling of this script, resolved the same way
# BUDGET_CHECK is, so a relocated checkout moves both together.
HYGIENE_EMITTER="${HYGIENE_EMITTER:-$(dirname "${BASH_SOURCE[0]}")/emit_box_health_hygiene.py}"

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

# The Overseer alert-drain's own artifacts, read to detect a drain that is
# SCHEDULED-OFF or hung rather than one that merely died (alpha-engine-
# config-I7858). See check_alert_drain_liveness() below for why this reads
# _control/completed/ rather than SQS depth directly.
OVERSEER_RESEARCH_BUCKET="alpha-engine-research"
# EventBridge fires 4x/day, every 6h (cron(0 4|10|16|22 * * ? *)). Two missed
# cycles plus a generous margin for a long-running drain (charter caps it at
# a 3h watchdog) without false-paging on ONE slow run.
ALERT_DRAIN_MAX_STALENESS_H=14
# The four EventBridge Scheduler schedules whose State distinguishes a drain
# that is DECLARED OFF from one that is hung (alpha-engine-config-I8679, Brian
# ruling 2026-08-26: "i don't want to be paged with box health at all if there
# is no issue"). Read with scheduler:GetSchedule, granted read-only on exactly
# these four ARNs by nous-ergon-ops infrastructure/iam/
# alpha-engine-dashboard-role/alpha-engine-dashboard-alert-drain-schedule-read.json.
ALERT_DRAIN_SCHEDULE_NAMES="alpha-engine-alert-drain-0400utc alpha-engine-alert-drain-1000utc alpha-engine-alert-drain-1600utc alpha-engine-alert-drain-2200utc"
# A pause is a state; a pause this long is a decision the operator owes. The
# console row says so past this bound — it still does not page, because a
# decision owed is Decision-Queue work, not a box-health incident. Mirrors
# PRODUCER_SUPPRESSION_MAX_DAYS in the freshness monitor.
ALERT_DRAIN_PAUSE_REVIEW_DAYS=14
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
# Declared empty BEFORE the source so a manifest rendered by an older
# generator degrades to "no HTTP liveness, reported" rather than aborting the
# whole watchdog on an unset array under `set -u`.
declare -A SERVICE_PORT=()
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
              llm-egress-proxy-anthropic.service \
              telos-web.service vires.service mnemon.service nousergon-auth.service \
              nousergon-console.service nginx.service)
    PORTS=(8501 8502 8503 8505 8000 3003 8506 3002 3001 8530 4100 5180 8980 8990 8971 443)
    EXPECTED_SERVICE_COUNT=16
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

# Minimum reclaim stall (memory.pressure `some avg60`, percent) that must
# accompany a throttle burst before it reaches the notification path.
#
# WHY A SECOND CONDITION EXISTS AT ALL. A MemoryHigh event is not damage. It is
# the kernel doing exactly what the soft cap asks: reclaim this cgroup's pages
# rather than let it grow. A service that touches its soft cap, gives pages
# back, and never stalls is operating AS DESIGNED — the cap is a throttle point,
# not a failure point. Measured on 2026-08-11: nousergon-console sat at
# memory.events `high 693` and climbing, with `full avg300=0.05` and 1535 MB
# free on the box. Nothing was degraded; the counter moved every tick; the
# finding published every tick.
#
# Sized from measurement, not intuition, and deliberately placed BETWEEN the
# two populations this box has actually produced:
#
#   0.00-0.05  background reclaim against a tight-but-working cap
#              (metron-api 2026-07-28, nousergon-console 2026-08-11)
#   49-55      a service wedged hard enough that no HTTP request completed
#              (vires 2026-08-03)
#
# 1.0 is 20x the observed no-harm background and 50x below the observed harm,
# so neither population lands near it. The separate `memory pressure:` check
# above still fires at `some avg10 > 10` and still pages at critical — this
# threshold only decides whether a THROTTLE BURST is worth reporting, and it
# cannot make that check quieter.
#
# WHAT THIS DOES NOT SUPPRESS, stated because a gate that hides a real finding
# is worse than the noise it removes: a cap pinned below its service's working
# set is still detected, by check_memory_budget.py's CENSORED reading, which
# renders the unit on the console headroom surface as state `censored` →
# envelope status `attention` on EVERY tick. That is the correct home for "this
# declared cap is wrong": a standing property of the budget, rendered on a
# surface, not an event re-published every ten minutes. This gate removes the
# duplicate on the notification path, not the detection. (config-I6859 was
# found and fixed from exactly that console state.)
CGROUP_STALL_MIN=1.0

# Seconds a service gets to produce an HTTP status line before it counts as
# not answering. Sized from measurement, not intuition: every healthy service
# on this box answered a bare GET in 0.001-0.027s on 2026-08-03, and the
# wedged one did not answer in 20. Three orders of magnitude separate the two
# populations, so 3s sits nowhere near either — it is long enough that a
# cold-start or a GC pause cannot trip it and short enough that a fully dead
# box still finishes its confirmation sweep inside the 10-minute tick
# (worst case 13 units x 3s x RETRY_ATTEMPTS).
HTTP_PROBE_TIMEOUT=3

# Where the previous run's throttle counters live. /var/lib, not /tmp: a
# tmpfiles cleanup mid-window would silently re-baseline and hide throttling.
#
# $STATE_DIRECTORY is exported by systemd from StateDirectory= in the unit,
# which is what makes the directory exist AND be writable by User=ec2-user.
# The literal fallback is for running the script by hand outside systemd.
THROTTLE_STATE_DIR="${STATE_DIRECTORY:-/var/lib/box-health}"
THROTTLE_STATE="${THROTTLE_STATE_DIR}/cgroup-high-counts"

# Which conditions this box has ALREADY ALERTED ON, carried across ticks so a
# condition that ends can emit its terminator (alpha-engine-config-I8105).
#
# WHY A NEW FILE AND NOT THE CONFIRM-ON-RETRY SET. The issue's premise was that
# "the script already keeps the confirmed-problems set across ticks"; measured
# while implementing this, it does not. `confirmed` is intersected across the
# RETRY_ATTEMPTS samples of ONE run and then discarded at exit — there has
# never been any cross-tick memory of what was alerted. Without one, the set
# difference the clear needs has nothing on its left-hand side: every tick
# starts from an empty prior and every condition looks new. So the memory is
# what actually had to be built; the diff is the easy half.
#
# Same /var/lib rationale as THROTTLE_STATE above: a tmpfiles sweep of /tmp
# mid-window would erase the prior, and an erased prior is silently
# indistinguishable from "nothing was alerted", which suppresses every clear
# that was due.
#
# Format: one TAB-separated row per problem LINE —
#   <identity_key>\t<severity>\t<problem line>
# Rows sharing an identity_key were carried by ONE page (the set-derived
# publishes group several findings into one message). The identity key is the
# unit of clearing, not the line: a page's key is derived from the SET it
# carried, so the key surviving means that same page is still standing, and
# the key disappearing means that page's condition is over. That is the same
# identity krepis dedups on, deliberately — a clear keyed on anything else
# would pair to a page nobody sent.
ALERTED_STATE="${THROTTLE_STATE_DIR}/alerted-problems"

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
#   $8 triggered service's ActiveState  $9 the finding this unit carried on the
#      PREVIOUS run, verbatim, or empty
classify_timer_staleness() {
    local name="$1" now="$2" last="$3" budget="$4" result="$5" age
    local fail_since="${6:-}" next_elapse="${7:-}"
    local active_state="${8:-}" prior_finding="${9:-}"

    # ── A RUN IN FLIGHT MAKES `Result` MEANINGLESS (alpha-engine-config-I8359) ──
    #
    # systemd RESETS `Result` to `success` when a unit starts and only sets the
    # real outcome when it finishes. Measured on i-09b539c844515d549:
    #
    #   BEFORE:  Result=success  ActiveState=inactive    SubState=dead
    #   DURING:  Result=success  ActiveState=activating  SubState=start
    #
    # So a timer that has been failing for days reads as HEALTHY for the whole
    # duration of its next attempt. The finding leaves the confirmed set, and
    # because all four confirm-on-retry samples fall inside that same run, the
    # retry window confirms its ABSENCE rather than catching the flap.
    #
    # What that cost, measured 2026-08-25: `health_problems_unalerted` went
    # 2.0, 2.0, **0.0**, 2.0, 2.0 across five consecutive ticks, and
    # `alpha-engine-dashboard-health-problems` sent an OK email at 18:23:27 UTC
    # for a condition that never ended, returning to ALARM 20 minutes later.
    # The journal for that tick names both keys as `clear DUE`.
    #
    # Until 2026-08-25 the clears could not be delivered at all (the krepis pin
    # lagged the call site, I8105), so the only symptom was the flapping alarm.
    # With the pin restored those clears DO deliver, which turns a silent flap
    # into an affirmative "condition resolved" page for a standing condition.
    # That is why this is corrected rather than tolerated.
    #
    # CARRY, do not re-derive. A run in flight is not evidence of success and
    # not evidence of failure -- it is an absence of evidence, and the honest
    # response is to keep reporting what was last actually measured. Re-emitting
    # the prior line VERBATIM keeps the identity key byte-identical, so nothing
    # clears, the level holds, and the metric does not flap.
    #
    # Self-correcting in the safe direction: the moment the run finishes,
    # `Result` is real again -- success clears the finding on the very next
    # tick, another failure is correctly a NEW page for a NEW run. The only
    # cost of being wrong here is holding a resolved finding for the length of
    # one run; the cost of the opposite was an all-clear for a live condition.
    case "$active_state" in
        activating|active|reloading|deactivating)
            [ -n "$prior_finding" ] && echo "$prior_finding"
            # The staleness half is skipped too: a unit that is running right
            # now is by definition not overdue, and evaluating it against a
            # budget mid-run is the same absence-of-evidence error.
            return 0
            ;;
    esac

    # Execution outcome. FIRST, and before any coverage guard, because it needs
    # NOTHING from budget.yaml: a job can fail promptly and on schedule forever,
    # which is stale by no measure and broken by any.
    #
    # This used to sit BELOW the no-budget guard, which returned early. So a
    # timer installed without a `timers:` row — the exact state a brand-new
    # timer is in — had its outcome check skipped, and the watchdog reported the
    # missing row instead of the failure. Live on 2026-07-29: metron-intraday
    # had failed 48 of 48 runs on an S3 AccessDenied and box_health.sh said only
    # "add a timers: row to budget.yaml". The coverage-hole branch was
    # suppressing the very check it was a hole in — the guard-excludes-the-class-
    # it-protects shape, again, and the reason the ordering here is load-bearing
    # rather than stylistic. Asserted in test_box_health_timer_staleness.sh.
    if [ -n "$result" ] && [ "$result" != "success" ]; then
        # The failing run's OWN timestamp and the timer's next scheduled
        # attempt, both raw systemd calendar strings (stable across ticks --
        # neither changes until the run itself changes). This is what makes a
        # repeat page recognisable as the SAME failing run rather than a new
        # one (alpha-engine-config-I7677). Deliberately NOT a computed
        # relative age ("3h ago"): that would change every 10-min tick, which
        # would defeat both confirm-on-retry stability and the identity-keyed
        # dedup below.
        local detail=""
        [ -n "$fail_since" ] && detail="${detail}, failing run started ${fail_since}"
        [ -n "$next_elapse" ] && detail="${detail}, next attempt ${next_elapse}"
        echo "timer job failing: $name (last run result=$result${detail})"
    fi

    # No declared budget = the STALENESS half is unmonitored. NAMED, not
    # skipped: a coverage hole that stays quiet is how this class of gap
    # presents as green.
    #
    # `notice:` because of what remains covered without the row. The scheduler
    # check (classify_timer) still sees a timer that stopped firing, and the
    # outcome check above still sees one that fails. What is missing is only the
    # narrow case of a timer that is active, has a valid next elapse, and whose
    # last trigger is nonetheless ancient — a mis-edited OnCalendar. Real, worth
    # fixing, and not a reason to push a phone notification.
    #
    # THE REMEDY NAMES BOTH PATHS (alpha-engine-config-I8034). A timer this repo
    # installs takes a `timers:` row in budget.yaml, which CI already enforces
    # (tests/test_every_installed_timer_has_a_deadman_row.py). A timer installed
    # by nous-ergon-ops, metron or the-cyphering cannot be enforced from here and
    # used to need a second, hand-made edit in this repo that six separate
    # findings show nobody reliably makes -- so it declares
    # `X-DeadManStaleness=` in its own [Unit] section and travels with it.
    # generate-box-manifest.py merges both into TIMER_MAX_STALENESS.
    #
    # Naming only the budget.yaml half sent every reader of this line to the
    # wrong repo, which is most of why the finding kept recurring with a new
    # timer name in it.
    if [ -z "$budget" ]; then
        echo "notice: timer has no dead-man threshold: $name — add X-DeadManStaleness= to its [Unit] section (any repo), or a timers: row to budget.yaml"
        return
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

# timer_failure_dedup_key UNIT RESULT INACTIVE_EXIT_RAW
#
# Identity for ONE "timer job failing" finding, keyed on (unit, Result,
# InactiveExitTimestamp) rather than on message text plus a cooldown
# (alpha-engine-config-I7677). `systemctl show <unit> -p Result` is a LEVEL,
# not an event -- it stays e.g. `exit-code` until the unit's NEXT run, so a
# text/cooldown dedup on "timer job failing: ..." re-fires every cooldown
# window for as long as the timer's interval, up to 7 days for a weekly timer,
# on ONE already-fixed failure. Keying on the run's own identity instead means
# the SAME failing run pages once: the key only changes when
# InactiveExitTimestamp advances, which happens exactly when the timer runs
# again (success clears it; another failure is correctly a NEW page for a NEW
# run).
#
# Pure (date -d is a deterministic function of its argument, not of wall
# clock) so this is unit-testable without systemd -- see
# test_box_health_timer_staleness.sh.
timer_failure_dedup_key() {
    local unit="$1" result="$2" ts_raw="$3" ts_epoch=""
    [ -n "$ts_raw" ] && ts_epoch=$(date -d "$ts_raw" +%s 2>/dev/null)
    printf 'boxhealth-critical-timerfail-%s' \
        "$(printf '%s-%s-%s' "$unit" "${result:-unknown}" "${ts_epoch:-$ts_raw}" | tr ' /:' '___')"
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
#   $5 reclaim stall — memory.pressure `some avg60` percent, EMPTY if unreadable
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
    local name="$1" current="$2" baseline="$3" floor="$4" stall="${5-}" delta

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

    [ "$delta" -ge "$floor" ] || return

    # ── the harm gate (CGROUP_STALL_MIN) ─────────────────────────────────────
    # An unreadable stall reading REPORTS. Absence of evidence is not evidence
    # of absence, and this whole file's recurring defect has been a check whose
    # silent path was also its broken path (I4512's unparseable PSI predicate,
    # the unwritable state dir, the User=ec2-user mkdir). A gate that fails
    # closed would join that list.
    if [ -n "$stall" ] && awk -v v="$stall" -v m="$CGROUP_STALL_MIN" \
            'BEGIN{exit !(v+0 < m+0)}' 2>/dev/null; then
        # Reclaim with no measurable cost. The journal keeps the numbers so the
        # gate's own decision is reconstructible from the box alone; the console
        # headroom row keeps the standing budget finding.
        printf 'box_health: %s throttle detail: %sx MemoryHigh since last check, some avg60=%s%% — below CGROUP_STALL_MIN=%s, not reported\n' \
            "$name" "$delta" "$stall" "$CGROUP_STALL_MIN" >&2
        return
    fi

    # STATIC problem text. The delta is deliberately NOT in this line: the alert
    # dedup key is derived from the problem SET (publish_problems --dedup-key),
    # so a count that changes every tick produces a new key every tick and the
    # 60-minute dedup window never suppresses anything. One standing condition
    # re-published as a new alert on every sample is what "33x", "51x", "56x"
    # were — three notifications about one undersized cap. Same rule, same
    # reason, as the `memory pressure:` line above, which moves its live
    # percentage to the journal for the sibling reason (confirmation
    # intersection). The numbers live in the journal line below.
    printf 'box_health: %s throttle detail: %sx MemoryHigh since last check, some avg60=%s%%\n' \
        "$name" "$delta" "${stall:-unreadable}" >&2
    if [ -z "$stall" ]; then
        # Distinct text, because the two states are distinct findings and a
        # single string would make the journal the only place to tell them
        # apart. This one is a watchdog-coverage statement: the gate could not
        # be evaluated, so the burst is reported unjudged.
        echo "cgroup throttle: $name is throttling against its MemoryHigh cap; stall reading unavailable"
    else
        echo "cgroup throttle: $name is throttling against its MemoryHigh cap with measurable reclaim stall"
    fi
}

# emit_hygiene_envelope LINES — the info tier's delivery path.
#
# Called on EVERY exit path, including the all-healthy early exits, with an
# empty argument when there is nothing to report. principles.md §7: a component
# emitting nothing is not healthy, it is unobserved, and a surface that
# publishes only when something is wrong cannot be distinguished from one that
# has died.
#
# rc=3 (the emitter could not publish) goes to the journal and no further. This
# is a RENDERING path: a rendering failure must not manufacture a box-health
# problem, and the console already shows a missing artifact as `unreadable`
# rather than `ok`, so the gap stays visible where it belongs. Same contract as
# the --emit-check call above, deliberately.
emit_hygiene_envelope() {
    [ -x "$VENV_PY" ] || { echo "box_health: hygiene envelope skipped, no venv python ($VENV_PY)" >&2; return 0; }
    [ -r "$HYGIENE_EMITTER" ] || { echo "box_health: hygiene emitter missing ($HYGIENE_EMITTER)" >&2; return 0; }
    printf '%s\n' "${1:-}" | "$VENV_PY" "$HYGIENE_EMITTER" >/dev/null || true
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

# UNALERTED_CRITICALS — how many critical problem LINES this run found and then
# could not deliver through krepis.alerts. Incremented by publish_problems.
UNALERTED_CRITICALS=0

# publish_unalerted COUNT — the metric the CloudWatch backstop actually alarms
# on (alpha-engine-config-I8035).
#
# WHY THIS EXISTS, AND WHY health_problems COULD NOT BE IT. publish_verdict
# above is a LEVEL: it republishes the current critical count every tick. Two
# of its inputs are themselves levels — `timer job failing:` is derived from
# `systemctl show -p Result`, which stays `exit-code` until the unit's NEXT
# run. So ONE failing daily timer holds the level for ~144 consecutive ticks.
#
# Measured on i-09b539c844515d549 over 274 runs, 2026-08-07..21:
#   `timer job failing: ops-config-drift.timer`  175 ticks  <-  3 actual failures
# and `alpha-engine-dashboard-health-problems` transitioned ALARM<->OK 15 times
# in 13 days. Both AlarmActions and OKActions point at the alpha-engine-alerts
# SNS topic, so that is 30 emails for 3 failures — on top of the krepis.alerts
# page that had already been delivered for each one.
#
# THE ALERT PATH ALREADY SOLVED THIS. timer_failure_dedup_key (config-I7677)
# keys each timer finding on (unit, Result, InactiveExitTimestamp) precisely so
# that one failing RUN pages once rather than once per cooldown window. That fix
# was never inherited here, because this path deliberately does not look at the
# alert path at all.
#
# The resolution keeps both properties instead of trading one for the other.
# `health_problems` stays exactly what it was — an independent level, published
# every run, carrying no alarm action, readable on the console and in history.
# `health_problems_unalerted` counts only criticals whose `krepis.alerts publish`
# FAILED, and the alarm moves onto it. That is the condition the alarm's own
# description has always named:
#
#   "box_health.sh confirmed >=1 problem it may not have been able to alert
#    about (config-I5211)"
#
# I5211's argument survives intact: this is still a second, independent code
# path, and the case it was built for — a silently no-op alerts module, the
# config#1646 class — makes every publish fail, so this metric goes non-zero and
# the alarm fires. What stops is the duplication of pages that DID land.
#
# Published on EVERY exit path including the all-healthy ones (principles.md
# §7): a series that stops must stay distinguishable from a healthy zero, which
# is the same contract publish_verdict already keeps.
#
# KNOWN LIMIT, stated rather than implied: if box_health.sh dies before reaching
# a publish, neither metric updates and the alarm sees missing data, which is
# `notBreaching` by design. That silence is covered by box-health.timer's
# dead-man row in budget.yaml, not here — two alarms breaching on one silence is
# the double-page this file already refuses elsewhere.
publish_unalerted() {
    aws cloudwatch put-metric-data --namespace "AlphaEngine/Box" \
        --metric-data \
        "MetricName=health_problems_unalerted,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}],Value=${1:-0},Unit=Count" \
        2>&1 | head -1 | sed 's/^/box_health: unalerted publish failed: /' >&2 || true
}

# ── Condition lifecycle: emit the terminator (alpha-engine-config-I8105) ────
#
# THE DEFECT THIS CLOSES. Every alert this script emitted was write-once: a
# CRITICAL on detection and NOTHING when the condition ended. Measured
# 2026-08-21 on i-09b539c844515d549 — litellm-config-reconcile.timer failed
# 18:40:28 and 18:50:28 UTC and recovered 18:53:12 (14 green runs since);
# ops-config-drift.timer failed 20:02:30 and 20:09:49 and recovered 20:23:47.
# Three CRITICAL pages, zero all-clears. Every page was CORRECT when sent —
# each failure survived its own next scheduled attempt, so confirm-on-retry
# behaved exactly as designed, and nothing about detection is changed here.
# What was missing is the other end of the record, and without it a human
# triaging a digest has to re-measure every condition by hand, the alert-drain
# can never learn a condition ended, and the console's last known state is
# permanently the failure.
#
# NOT A SECOND PROSE EMAIL. The clear rides krepis.alerts' open/clear
# primitive: state=cleared plus the ORIGINATING PAGE'S identity key on the
# nousergon.alert.v1 event, so alert_drain_ingest.py pairs page to clear on a
# field rather than by string-matching prose. It is published at `info` and
# silently (delivered, no phone push): a clear that buzzes at 8pm is a second
# alert, not a resolution.

# Rows for the conditions published THIS run, accumulated by publish_problems.
# Initialised (not merely declared) — `set -u` turns an unset accumulator into
# a dead watchdog, the same way it did for undeclared_state above.
ALERTED_NOW=""
# Count of clears that were DUE and could not be published. Its own series:
# a missing terminator is invisible by construction, so the only way it is not
# a silent regression is a number whose non-zero is the finding.
UNPUBLISHED_CLEARS=0

# krepis_supports_clear — does the INSTALLED krepis carry the open/clear pair?
#
# A CAPABILITY PROBE, NOT A VERSION COMPARISON. This box installs krepis from a
# pinned requirement (requirements.txt), which lags the library by design, and
# the alerts CLI's `clear` subcommand on a krepis without it exits 2 from
# argparse (this sentence deliberately does not spell the invocation out: the
# fleet alert-source scanner matches the `-m <module>` adjacency in PROSE as
# well as in code, and a comment is not an emitter)
# — indistinguishable at the call site from a real delivery failure, and it
# would drive UNPUBLISHED_CLEARS non-zero for a version skew rather than for a
# fault. Asking the module what it has is exact, costs one interpreter start on
# the non-clean path only, and self-heals the moment the pin moves: no clear is
# lost, because a condition still standing is still in the state file.
krepis_supports_clear() {
    "$ALERT_PY" -c 'import krepis.alerts as a; raise SystemExit(0 if hasattr(a, "publish_clear") else 1)' \
        </dev/null >/dev/null 2>&1
}

# krepis_supports_publish_lifecycle — the SAME probe, for the OTHER half of the
# lifecycle pair (alpha-engine-config-I8105 follow-up).
#
# WHY THIS WAS MISSING AND WHAT IT COST. `krepis_supports_clear` above reasons
# correctly that a version skew must not be reported as a delivery failure —
# and then guards only the `clear` call. The lifecycle arguments added to the
# PUBLISH call in the same change got no such probe, so the two halves of one
# feature degraded in opposite directions: a skewed clear said so and carried
# on, a skewed publish exited 2 from argparse and was counted as a page nobody
# received.
#
# Measured on i-09b539c844515d549 from 2026-08-22 01:41 to 2026-08-25, every
# 10-minute tick:
#
#   <the alerts CLI>: error: unrecognized arguments: [the two lifecycle
#   flags]  ->  box_health: critical publish failed   (x2 criticals, 1 warning)
#
# (That transcript deliberately does not spell the module invocation out, for
# the reason krepis_supports_clear states above: the fleet alert-source scanner
# matches the `-m <module>` adjacency in PROSE as well as in code, and reads a
# comment as a sourceless emitter.)
#
# health_problems_unalerted sat at 2-3 for three days and the CloudWatch
# backstop stayed in ALARM. The backstop was RIGHT — the alert path really was
# broken — but the break was a pin lagging a call site, and both real findings
# behind it (llm-capability-probe.timer, ops-config-drift.timer) reached nobody
# for three days. A degrade here would have delivered them, without the
# lifecycle metadata, and said so.
#
# Probes the FUNCTION SIGNATURE rather than the CLI: argparse's flags are
# derived from it, one interpreter start answers both, and a parser error is
# exactly what this exists to avoid provoking.
#
# NOT A SILENT SWALLOW. (a) The failure mode is "the installed krepis predates
# the lifecycle pair"; (b) the page itself — the primary deliverable — is
# published without the lifecycle arguments, which is precisely how every page
# was published before I8105; (c) it is recorded on stderr, captured by
# journald, and the pin-lag condition is independently a FINDING from
# check-krepis-venv-drift.sh once crucible-dashboard declares its floor.
krepis_supports_publish_lifecycle() {
    "$ALERT_PY" -c 'import inspect, krepis.alerts as a; raise SystemExit(0 if {"state", "identity_key"} <= set(inspect.signature(a.publish).parameters) else 1)' \
        </dev/null >/dev/null 2>&1
}

# Resolved at most once per run, on the non-clean path only: the probe costs an
# interpreter start and publish_problems can be called several times.
#   1 = supported, 0 = not, empty = not yet asked.
KREPIS_PUBLISH_LIFECYCLE=""
krepis_publish_lifecycle_args() {
    if [ -z "$KREPIS_PUBLISH_LIFECYCLE" ]; then
        if krepis_supports_publish_lifecycle; then
            KREPIS_PUBLISH_LIFECYCLE=1
        else
            KREPIS_PUBLISH_LIFECYCLE=0
            echo "box_health: installed krepis predates the publish lifecycle pair — paging WITHOUT it (bump the krepis pin; see infrastructure/krepis-floor.txt)" >&2
        fi
    fi
    [ "$KREPIS_PUBLISH_LIFECYCLE" = "1" ]
}

# alerted_state_prior — rows written by the previous run, or empty.
# Empty is a valid, expected state (first run after a deploy, reboot, or a
# recreated state dir) and means exactly "nothing was alerted": everything
# found this run is `opened`, and no clear is due. It never means "clear
# everything".
alerted_state_prior() {
    [ -r "$ALERTED_STATE" ] || return 0
    cat "$ALERTED_STATE" 2>/dev/null
}

# alerted_timer_finding UNIT — the "timer job failing:" line this unit carried
# on the PREVIOUS run, or empty.
#
# Reads the same state file the lifecycle diff uses, matching on the timerfail
# key's unit segment rather than on message text: the message embeds a
# timestamp and a next-attempt time, so a text match would be a moving target
# while the key's unit segment is stable. Used only on the mid-run path in
# classify_timer_staleness (alpha-engine-config-I8359).
#
# Stable within a run -- the file is not rewritten until alerted_state_write on
# the way out -- so all four confirm-on-retry samples see the same answer,
# which is what keeps the carried line confirmable.
alerted_timer_finding() {
    local unit="$1"
    alerted_state_prior | awk -F'\t' -v k="boxhealth-critical-timerfail-${unit}-" \
        'index($1, k) == 1 { print $3; exit }'
}

# alerted_state_lifecycle KEY — `still_open` if the previous run alerted on
# this exact identity key, `opened` otherwise. Pure function of the state file
# plus its argument, so it is testable without systemd or S3.
alerted_state_lifecycle() {
    local key="$1"
    if alerted_state_prior | cut -f1 | grep -qxF "$key"; then
        echo still_open
    else
        echo opened
    fi
}

# alerted_state_write ROWS — atomic swap, same reason throttle_baseline_write
# uses one: a half-written prior produces a garbage diff on the next run, and a
# garbage diff here means either a clear for a live condition or no clear at
# all for one that ended.
alerted_state_write() {
    local tmp
    mkdir -p "$THROTTLE_STATE_DIR" 2>/dev/null || return 0
    tmp="${ALERTED_STATE}.$$"
    printf '%s' "$1" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
    mv -f "$tmp" "$ALERTED_STATE" 2>/dev/null || rm -f "$tmp"
}

# publish_clears — emit one terminator per identity key that was alerted on
# last run and is NOT alerted on this run.
#
# THE DIFF IS TAKEN ON THE KEY, NOT ON THE MESSAGE TEXT. The key already IS the
# condition's identity: for the set-derived tiers publish_problems derives it
# from the problem set, so a set that changed at all is a different page; for
# timer findings it is (unit, Result, InactiveExitTimestamp) per
# alpha-engine-config-I7677, deliberately not a computed relative age. A key
# present then and absent now is therefore exactly "that page's condition is
# over", which is the only claim a clear is allowed to make.
publish_clears() {
    local prior_rows="$1" current_rows="$2"
    [ -n "$prior_rows" ] || return 0

    local gone_keys
    gone_keys=$(comm -23 \
        <(printf '%s\n' "$prior_rows" | cut -f1 | grep -v '^$' | sort -u) \
        <(printf '%s\n' "$current_rows" | cut -f1 | grep -v '^$' | sort -u))
    [ -n "$gone_keys" ] || return 0

    local supported=1
    krepis_supports_clear || supported=0

    local key lines msg _cl
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        # Every line that page carried, so the clear names what ended rather
        # than only that something did.
        lines=$(printf '%s\n' "$prior_rows" | awk -F'\t' -v k="$key" '$1==k {print $3}')
        msg="dashboard EC2 (${INSTANCE_ID}) health alert resolved:"
        while IFS= read -r _cl; do
            [ -n "$_cl" ] || continue
            msg="$msg"$'\n'" - $_cl"
        done <<< "$lines"
        if [ "$supported" -eq 0 ]; then
            echo "box_health: clear DUE but installed krepis has no publish_clear (bump the krepis pin) — key=$key" >&2
            UNPUBLISHED_CLEARS=$((UNPUBLISHED_CLEARS + 1))
            continue
        fi
        # </dev/null: this loop is fed by a here-string, and a child that
        # reads stdin would eat the remaining keys — every clear after the
        # first would silently never be attempted.
        if ! "$ALERT_PY" -m krepis.alerts clear \
            --message "$msg" \
            --identity-key "$key" \
            --source box-health </dev/null; then
            echo "box_health: clear publish failed for key=$key" >&2
            UNPUBLISHED_CLEARS=$((UNPUBLISHED_CLEARS + 1))
        fi
    done <<< "$gone_keys"
}

# publish_unpublished_clears COUNT — the series that makes a missing
# terminator visible. Zero is published too, on every run: a gauge that only
# appears when it is non-zero cannot be distinguished from a dead emitter, and
# this file already refuses that shape for publish_verdict.
publish_unpublished_clears() {
    aws cloudwatch put-metric-data --namespace "AlphaEngine/Box" \
        --metric-data \
        "MetricName=health_clears_unpublished,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}],Value=${1:-0},Unit=Count" \
        2>&1 | head -1 | sed 's/^/box_health: clears-unpublished publish failed: /' >&2 || true
}

# finalize_alert_lifecycle — diff, emit the clears, persist the new prior.
#
# CALLED ON EVERY EXIT PATH INCLUDING THE CLEAN ONE, and the clean one is the
# whole point: an all-healthy tick is precisely when yesterday's page ended.
# Wiring this only into the problems path would leave the most common recovery
# — the box going green — the one case that still emits nothing.
finalize_alert_lifecycle() {
    local prior
    prior=$(alerted_state_prior)
    publish_clears "$prior" "$ALERTED_NOW"
    alerted_state_write "$ALERTED_NOW"
    publish_unpublished_clears "$UNPUBLISHED_CLEARS"
}

# snapshot_problems — run the full check ONCE, printing one problem per line.
# No shared state; the caller samples it repeatedly and keeps the intersection.
# http_liveness_problems — emit a problem line per service that is listening
# but not answering. Its own function, not an inline block, so
# tests/test_box_health_http_liveness.py can extract and RUN it against real
# servers: a loop proven only by reading it is a loop nobody has run.
http_liveness_problems() {
    # ── HTTP liveness (alpha-engine-config-I6262) ──────────────────────────
    #
    # A LISTENING PORT IS NOT LIVENESS. The socket is bound by the kernel and
    # stays bound while the server behind it answers nothing at all, so the
    # port loop in snapshot_problems passes throughout the failure it most
    # needs to catch.
    #
    # Measured, 2026-08-03: vires.service sat wedged for ~18 minutes — pinned
    # at its cgroup MemoryHigh and stalled >50% of wall-clock on reclaim.
    # `curl -m 20 http://127.0.0.1:8530/health` from ON THE BOX returned 000
    # after timing out, while `systemctl is-active` said active, port 8530
    # was listening, and four consecutive ticks of this watchdog reported no
    # port problem. The outage was found by a human using the app.
    #
    # THE PREDICATE IS "DID IT ANSWER", NOT "DID IT RETURN 200". Measured on
    # this box the same day: of the thirteen HTTP ports, seven answer 200 on a
    # health route, five answer 404 on every candidate path (the Next.js apps
    # and nous-ergon-live serve under base paths), and nousergon-auth answers
    # 400 to a bare GET. All twelve are healthy. Requiring 200 would have
    # paged on five services that were working perfectly, which is how a check
    # gets tuned out. A wedged server is distinguishable without that: curl
    # reports http_code 000 because no status line ever arrived.
    #
    # Known limit, stated rather than implied: this cannot see a server that
    # answers a cheap route while its real work is blocked (an exhausted
    # worker pool behind a static handler). Catching that needs a per-service
    # deep health route, which is a bigger change than the hole it closes.
    #
    # Requires the manifest: the unit->port pairing lives there. The bare
    # SERVICES/PORTS arrays are two independent lists, NOT index-aligned (the
    # fallback block at the top of this file had signal.service sitting
    # opposite mnemon's port for months, harmlessly, because nothing paired
    # them). Pairing them by index here would name the wrong service in an
    # alert, so this check runs only against the generated map. MANIFEST_OK=0
    # is already reported loudly elsewhere.
    if [ "${MANIFEST_OK:-0}" -eq 1 ] && [ "${#SERVICE_PORT[@]}" -eq 0 ]; then
        # A manifest rendered by the pre-I6262 generator has no map, so this
        # check would cover nothing while appearing to run. Absence of a
        # signal is not health — say so instead of inheriting the silence.
        echo "watchdog: manifest carries no SERVICE_PORT map — HTTP liveness is UNMONITORED (re-run install-box-health.sh)"
    elif [ "${MANIFEST_OK:-0}" -eq 1 ]; then
        local unit port code scheme insecure
        for unit in "${!SERVICE_PORT[@]}"; do
            port="${SERVICE_PORT[$unit]}"
            # 443 is nginx terminating TLS with the Cloudflare origin cert,
            # which does not validate against 127.0.0.1 — -k, because this is
            # a liveness probe over loopback, not a certificate check.
            scheme=http; insecure=()
            if [ "$port" = "443" ]; then scheme=https; insecure=(-k); fi
            # NO `|| echo 000` here: -w '%{http_code}' ALREADY prints 000 when
            # the transfer fails, so appending a fallback yields "000000",
            # which matches nothing and silently disarms the check. That is
            # what the first version of this did, and it passed every fixture
            # test of the predicate — the defect lived in the pairing between
            # the predicate and its caller, so only running the real function
            # exposed it (test_shipped_function_names_the_wedged_service).
            code=$(curl -s "${insecure[@]}" -m "$HTTP_PROBE_TIMEOUT" \
                       -o /dev/null -w '%{http_code}' \
                       "${scheme}://127.0.0.1:${port}/" 2>/dev/null) || code=""
            [ -n "$code" ] || code=000   # curl absent or killed outright
            # Any status line means the server is answering. 000 means it
            # accepted the connection (or not) and never replied inside the
            # timeout — the wedge.
            if [ "$code" = "000" ]; then
                echo "service not answering HTTP within ${HTTP_PROBE_TIMEOUT}s: $unit"
            fi
        done
    fi
}

# check_alert_drain_liveness — emit a problem line if the Overseer alert-drain
# is SCHEDULED-OFF or hung, rather than merely dead. Its own function, not an
# inline block, for the same reason http_liveness_problems is: extractable and
# directly runnable by tests/test_box_health_alert_drain_liveness.py.
#
# WHY THIS EXISTS (alpha-engine-config-I7858). The existing
# alpha-engine-alert-drain-liveness-probe (nousergon-data) relaunches a DEAD
# spot box on EC2 instance-terminated / spot-interruption events. It has no
# opinion on a drain that never launches at all (the four
# alpha-engine-alert-drain-*utc EventBridge schedules were DISABLED under the
# 2026-08-07 pause, #6984, for 14 days with nothing paging on it — re-enabled
# 2026-08-21) or one that launches, runs, and exits `success` without doing
# anything (a silent consumption bug). This check is that missing backstop,
# independent of how the I7858 `warning`-tier routing question itself was
# resolved (#758 answered it by reclassifying the dominant offender to `info`
# rather than moving the whole tier off channel — this check stands on its
# own regardless: the alert-drain's liveness was never covered either way).
#
# WHY THIS READS _control/completed/, NOT SQS QUEUE DEPTH DIRECTLY. This
# box's IAM role (alpha-engine-dashboard-role) has broad read access to
# s3://alpha-engine-research (the `alpha-engine-research-access` policy) and
# no SQS or EventBridge Scheduler permissions at all — granting those is an
# IAM change, which belongs to nous-ergon-ops, not this repo (repository-
# tiering-policy). The drain already writes a completion marker to
# `overseer/_control/completed/alert-drain-<run_id>.json` on every run
# (`{"state":"success","rc":0,"run_id":...,"at":...}`) using credentials this
# box does not need to duplicate. A schedule that is disabled, or a run that
# hangs past its own 3h watchdog, both manifest identically here: no fresh
# completed marker. That does not distinguish "scheduled-off" from "hung"
# from "crashed", but the remedy for a human is the same investigation either
# way, and the existing spot-liveness probe already owns the death case.
#
# THE SECOND FAILURE MODE, AND HOW IT IS COVERED (alpha-engine-config-I8108).
# A run that fires on schedule, runs, and exits `{"state":"success","rc":0}`
# after reading ZERO messages from a genuinely non-empty queue is a consumption
# bug, not a scheduling one, and the staleness check above cannot see it — the
# marker lands on time. Alerting on `ingested == 0` alone is not the answer
# either: zero is also the correct, common reading for a quiet 6-hour window,
# and paging on every calm cycle is how a check gets ignored.
#
# Separating the two needs the queue's depth at run start, which this box's
# role cannot read (S3 on alpha-engine-research, no SQS, no Scheduler). So the
# PRODUCER publishes both halves into the completion marker: `queue_depth_before`
# (read from SQS by alert_drain_run.sh BEFORE the agent starts) and `ingested`
# (counted by the deterministic ingest wrapper's working-state file, not taken
# from the ledger the agent wrote). Neither passes through the agent's
# judgement, so this is not the drain grading its own homework — and it needs no
# IAM grant, which overseer-policy section 8 makes a never-autonomous change.
#
# UNMEASURED IS NOT HEALTHY. A marker with no `queue_depth_before`, no
# `ingested`, or an unparseable one reports as a `watchdog:` finding (warning) —
# never silence, and never folded into the critical the drain owns. "Could not
# check" and "checked and fine" are different answers and this check never
# collapses them; principles.md section 7 — no data is never rendered as green.
# alert_drain_declared_state — `disabled` / `enabled` / `mixed` / `unknown`.
#
# READS THE FACT, NOT THE DECLARATION. `automation_pause.json` in nousergon-data
# is the declaration and this box cannot read it (private repo, no checkout
# here); `automation_pause.py --check` already asserts declaration-vs-fact in
# both directions and is the right owner of that comparison. What this box
# needs is only the fact: are the schedules off. So there is no second copy of
# the manifest here to drift.
#
# UNKNOWN IS NOT ENABLED AND NOT DISABLED. A GetSchedule that fails — IAM
# drift, throttle, a renamed schedule — returns `unknown`, which reports as a
# `watchdog:` finding rather than defaulting to either side. Defaulting to
# `enabled` would page on every IAM hiccup; defaulting to `disabled` would
# silence a genuinely hung drain the moment this call broke. principles.md
# section 7: "could not check" and "checked and fine" are different answers.
alert_drain_declared_state() {
    local n st enabled=0 disabled=0 unknown=0
    for n in $ALERT_DRAIN_SCHEDULE_NAMES; do
        st=$(aws scheduler get-schedule --name "$n" --query State --output text 2>/dev/null)
        case "$st" in
            ENABLED)  enabled=$((enabled + 1)) ;;
            DISABLED) disabled=$((disabled + 1)) ;;
            *)        unknown=$((unknown + 1)) ;;
        esac
    done
    if [ "$unknown" -gt 0 ]; then
        echo unknown
    elif [ "$enabled" -gt 0 ] && [ "$disabled" -gt 0 ]; then
        echo mixed
    elif [ "$disabled" -gt 0 ]; then
        echo disabled
    else
        echo enabled
    fi
}

check_alert_drain_liveness() {
    local latest
    latest=$(aws s3api list-objects-v2 \
                 --bucket "$OVERSEER_RESEARCH_BUCKET" \
                 --prefix "overseer/_control/completed/alert-drain-drain-" \
                 --query "reverse(sort_by(Contents,&LastModified))[0].[Key,LastModified]" \
                 --output text 2>/dev/null)
    if [ -z "$latest" ] || [ "$latest" = "None" ]; then
        # A listing failure (network, throttle, IAM drift) is indistinguishable
        # from "no runs ever completed" at this call site. Both are watchdog
        # malfunctions worth a human looking at, same class as the df/timer
        # probes above — reported distinctly, not silently skipped and not
        # escalated to the drain's own critical (that would blame the drain
        # for this box's S3 access, not the drain's own health).
        echo "watchdog: cannot read alert-drain completion markers (S3 list failed or empty)"
        return 0
    fi
    local last_epoch now_epoch age_h
    last_epoch=$(date -d "$(printf '%s' "$latest" | awk '{print $2}')" +%s 2>/dev/null)
    if [ -z "$last_epoch" ]; then
        echo "watchdog: cannot parse alert-drain completion timestamp: $latest"
        return 0
    fi
    now_epoch=$(date +%s)
    age_h=$(( (now_epoch - last_epoch) / 3600 ))
    local key
    key=$(printf '%s' "$latest" | awk '{print $1}')
    if [ "$age_h" -ge "$ALERT_DRAIN_MAX_STALENESS_H" ]; then
        # EVERY string below is STATIC — no age, no key (alpha-engine-config-
        # I8678). Both move between ticks, and publish_clears derives this
        # tier's identity key from the problem SET, so "a set that changed at
        # all is a different page": an interpolated age made every hour open a
        # new condition and end the previous one, emitting one CRITICAL *and*
        # one RESOLVED per hour for as long as the condition stood. Exactly the
        # reasoning that kept computed relative age out of the timer identity
        # key (alpha-engine-config-I7677) and out of the sibling I8108 arm
        # below. The moving numbers go to the journal, which is where the
        # operator reads them anyway.
        local sched_state
        sched_state=$(alert_drain_declared_state)
        printf 'box_health: alert-drain last completed %sh ago (bound %sh, schedules %s): %s\n' \
               "$age_h" "$ALERT_DRAIN_MAX_STALENESS_H" "$sched_state" "$key" >&2
        # "scheduled-off or hung" is TWO answers and this used to page for
        # both. Brian ruling 2026-08-26 (alpha-engine-config-I8679): "i don't
        # want to be paged with box health at all if there is no issue." A
        # drain that is off because he turned it off is not an issue; a drain
        # that is on and not completing is.
        case "$sched_state" in
            disabled)
                # `notice:` -> info -> emit_hygiene_envelope ONLY. It never
                # reaches krepis.alerts, and it renders on the console on every
                # run including clean ones, with the finding's own age. Not
                # deleted, not silenced: principles.md section 7 — a paused
                # producer renders as an aged, visibly-not-green row, never as
                # nothing.
                if [ "$age_h" -ge $(( ALERT_DRAIN_PAUSE_REVIEW_DAYS * 24 )) ]; then
                    echo "notice: alert-drain declared off past its review bound — the pause is now a decision owed, see alpha-engine-config-I8679"
                else
                    echo "notice: alert-drain not consuming because it is DECLARED OFF — all four schedules DISABLED by ruling, see alpha-engine-config-I8679"
                fi
                ;;
            enabled)
                # The genuine finding this check was built for: the drain is
                # SUPPOSED to be running and no completed marker has appeared.
                echo "alert-drain not consuming: no completed run within the staleness bound while all four schedules are ENABLED — hung or crashed, see alpha-engine-config-I7858"
                ;;
            mixed)
                echo "watchdog: alert-drain schedules disagree — some ENABLED, some DISABLED, so the drain is neither paused nor running (detail in journal), see alpha-engine-config-I8679"
                ;;
            *)
                echo "watchdog: cannot read alert-drain schedule state (scheduler:GetSchedule failed or IAM drift) — paused-vs-hung is unmeasured, see alpha-engine-config-I8679"
                ;;
        esac
        # A stale marker already says everything; asserting on its stale
        # contents too would double-report one condition.
        return 0
    fi

    # ── consumption assertion (alpha-engine-config-I8108) ────────────────────
    local marker
    marker=$(aws s3 cp "s3://${OVERSEER_RESEARCH_BUCKET}/${key}" - 2>/dev/null)
    if [ -z "$marker" ]; then
        echo "watchdog: cannot read the alert-drain completion marker body (consumption unverified)"
        return 0
    fi
    # A canary drill deliberately never touches the queue — asserting on its
    # counts would page on every successful drill.
    case "$marker" in
        *'"state":"drill'*) return 0 ;;
    esac

    # Parsed with grep rather than a JSON reader on purpose: this function's
    # only dependencies today are aws/awk/date, and adding an interpreter adds
    # a "the check could not run" path to a check whose entire job is to be the
    # backstop. Safe because ANY parse miss falls through to the UNMEASURED
    # branch below — the failure direction is loud, never green.
    local depth ingested
    # [[:space:]]* after each colon: the PRODUCER's ingested-counts half comes
    # straight out of Python's `json.dumps(...)` (alert_drain_ingest.py
    # ingested-counts), whose DEFAULT separators are ", " and ": " — a space
    # after the colon. Measured 2026-08-26 on a real marker:
    # `"ingested":{"queue": 8, "fallback": 0}`. The original no-space pattern
    # never matched that shape, so `ingested` silently extracted empty and
    # every marker with real, non-null consumption read as UNMEASURED
    # (alpha-engine-config-I8108 regression — the exact "checked and fine" vs
    # "could not check" collapse this function exists to prevent, just
    # inverted: real data misread as absent). `queue_depth_before` is written
    # by this script's own printf with no space and would still match either
    # way; the space tolerance costs nothing and guards the same drift there.
    depth=$(printf '%s' "$marker" | grep -Eo '"queue_depth_before":[[:space:]]*[0-9]+' | head -1 | grep -Eo '[0-9]+$')
    ingested=$(printf '%s' "$marker" | grep -Eo '"ingested":\{"queue":[[:space:]]*[0-9]+' | head -1 | grep -Eo '[0-9]+$')
    if [ -z "$depth" ] || [ -z "$ingested" ]; then
        # Covers all three: the producer has not deployed the fields yet, the
        # run could not measure one of them (emitted as JSON null, which is
        # deliberately NOT 0), or the marker shape changed.
        echo "watchdog: alert-drain completion marker carries no queue-depth/ingested measurement (consumption unverified)"
        printf 'box_health: alert-drain marker without I8108 measurement: %s\n' "$key" >&2
        return 0
    fi
    if [ "$depth" -gt 0 ] && [ "$ingested" -eq 0 ]; then
        # STATIC string: the confirm-on-retry intersection matches lines
        # exactly, and depth moves between samples. Numbers go to the journal.
        echo "alert-drain not consuming: last run ingested 0 from a NON-EMPTY intake queue — silent consumption failure, see alpha-engine-config-I8108"
        printf 'box_health: alert-drain %s ingested %s of %s queued\n' "$key" "$ingested" "$depth" >&2
    fi
}

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

    # systemd services — active AND enabled checks.
    # A running service that is not `enabled` won't survive a reboot, and
    # nothing detects this until the box comes back without it (observed
    # live 2026-07-27: nousergon-auth was running but `disabled`, lost on
    # first reboot in 53 days — alpha-engine-config-I4790).
    # Alert-only, never auto-enable — a deliberately-disabled unit (e.g.
    # a service being decommissioned) must not be silently re-enabled.
    local s
    for s in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$s"; then
            systemctl is-enabled --quiet "$s" || echo "service running but NOT enabled: $s"
        else
            echo "service down: $s"
        fi
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
        local budget_out budget_rc
        budget_out=$("$VENV_PY" "$BUDGET_CHECK" --installed --quiet 2>&1)
        budget_rc=$?
        # Exit code carries the severity, because the two findings are not the
        # same event: 1 = the box is out of budget, 2 = the box is fine and
        # something is degrading our ability to measure it, 3 = the check could
        # not run. Collapsing 1 and 2 into one alert is what made the page rate
        # track bookkeeping instead of health.
        case "$budget_rc" in
            0) ;;
            1) echo "memory budget: BREACH (detail in journal)" ;;
            2) echo "notice: memory budget observation hygiene (detail in journal)" ;;
            *) echo "watchdog: memory budget check failed to run (rc=$budget_rc)" ;;
        esac
        if [ "$budget_rc" -ne 0 ]; then
            printf 'box_health: memory budget detail (rc=%s):\n%s\n' "$budget_rc" "$budget_out" >&2
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
    local svc last_epoch now_epoch result budget staleness_ok inactive_exit_raw svc_active
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
        # Failing-run identity (alpha-engine-config-I7677): only fetched when
        # there IS a triggered service, same guard as Result above.
        inactive_exit_raw=""
        [ -n "$svc" ] && inactive_exit_raw=$(systemctl show "$svc" -p InactiveExitTimestamp --value 2>/dev/null)
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
        # ActiveState of the TRIGGERED SERVICE, for the mid-run guard in
        # classify_timer_staleness (alpha-engine-config-I8359). Same guard as
        # Result above: only meaningful when there is a triggered service.
        svc_active=""
        [ -n "$svc" ] && svc_active=$(systemctl show "$svc" -p ActiveState --value 2>/dev/null)
        classify_timer_staleness "$t" "$now_epoch" "$last_epoch" "$budget" "$result" \
            "$inactive_exit_raw" "$next_real" "$svc_active" "$(alerted_timer_finding "$t")"
    done

    # ── per-service cgroup memory pressure (alpha-engine-config-I4512) ─────
    # The 2026-07-27 failure: two services (litellm-proxy, llm-egress-proxy)
    # were pinned at their MemoryHigh ceiling with memory.pressure ~60% and
    # memory.events high in the thousands, yet nothing surfaced this until
    # they failed to restart. memory.pressure some avg10 > 10 means the
    # service is spending >10% of time stalled on reclaim — a sustained
    # throttle. memory.events high > 0 means the cgroup has hit its soft
    # limit since boot.
    local cg evt pressure stall high_count throttle_state_seen=0
    for s in "${SERVICES[@]}"; do
        stall=""
        # cgroup v2 uses literal unit names under system.slice/ — hyphens and
        # dots are NOT hex-escaped for service units (confirmed on the actual
        # box 2026-07-28).  Hex escaping (e.g. \x2d) is only for slice unit
        # names where the prefixed "system-" separator must be distinguishable
        # from a hyphen in the unit's own name.
        cg="/sys/fs/cgroup/system.slice/${s}/memory.pressure"
        if [ -r "$cg" ]; then
            # PSI fields are KEY=VALUE, not whitespace-separated columns:
            #
            #   some avg10=55.27 avg60=53.82 avg300=51.32 total=525043914
            #
            # so $2 is the string "avg10=55.27" and `$2+0` is 0 in awk, for
            # every value this check exists to catch. The predicate `val>10`
            # was therefore false unconditionally, from the day it shipped
            # (alpha-engine-config-I4512) until 2026-08-03 — a detector whose
            # parser made it structurally incapable of firing.
            #
            # Measured, not inferred: on 2026-08-03 vires.service sat at
            # `some avg10=55.27 / full avg300=49.93` for ~18 minutes, wedged
            # hard enough that no HTTP request completed, and this check
            # emitted nothing across four consecutive 10-minute ticks. Proven
            # against a real PSI fixture in
            # tests/test_box_health_memory_pressure_check.py, which fails
            # against the pre-fix expression.
            pressure=$(awk '/^some /{split($2,kv,"="); v=kv[2]+0; if (v>10) printf "%.2f", v}' "$cg" 2>/dev/null)
            # avg60, not the avg10 the critical check uses. The throttle gate
            # asks a different question — "did this burst cost the service
            # anything?" — over the window the burst was counted in (a 10-min
            # tick), so the shorter average would let a burst that has already
            # subsided read as harmless. Field 3, since PSI orders
            # `some avg10= avg60= avg300= total=`.
            stall=$(awk '/^some /{split($3,kv,"="); printf "%.2f", kv[2]+0}' "$cg" 2>/dev/null)
            if [ -n "$pressure" ]; then
                # The live percentage goes to the JOURNAL, never into the
                # problem line. snapshot_problems is sampled RETRY_ATTEMPTS
                # times and confirms only lines present in EVERY sample, and
                # a PSI average moves between samples — so embedding it made
                # this line unable to confirm even once the predicate worked.
                # Second independent reason the same check could never page;
                # same class as test_box_health_budget_wiring's static-string
                # rule, which only covered the `memory budget:` prefixes.
                printf 'box_health: %s memory pressure detail: some avg10=%s%%\n' \
                    "$s" "$pressure" >&2
                echo "memory pressure: $s is stalled on reclaim against its memory cap"
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
                "$(throttle_baseline "$s")" "$CGROUP_HIGH_DELTA_MIN" "$stall"
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
    unset cg evt pressure stall high_count throttle_state_seen

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

    http_liveness_problems
    check_alert_drain_liveness
}

# Gauges flow on every tick regardless of health outcome (see emit_metrics).
emit_metrics

# Per-unit memory headroom onto the console surface (config-I5863).
#
# ONCE PER RUN, HERE, AND NOT INSIDE snapshot_problems. snapshot_problems is
# sampled up to RETRY_ATTEMPTS times for confirmation; publishing from there
# would write the envelope four times per tick and, worse, would tie the console
# row to the confirmation intersection — so a condition that self-heals within
# the window would leave the console showing nothing at all, which is the state
# the surface exists to make impossible.
#
# BEFORE the EXIT trap re-baselines the throttle counters, so the delta this
# publishes is the same one classify_throttle_delta alerts on. Reversing these
# two lines makes every published delta zero, and a zero delta is exactly what a
# quiet box looks like.
#
# Unconditional on health, like emit_metrics above: a surface that publishes
# only when something is wrong cannot be distinguished from one that has died.
if [ -r "$BUDGET_CHECK" ]; then
    "$VENV_PY" "$BUDGET_CHECK" --emit-check --quiet >/dev/null 2>&1
    emit_check_rc=$?
    # 0, 1 and 2 are budget VERDICTS. They are already reported by the
    # --installed invocation inside snapshot_problems; treating them as failures
    # here would print an error line on every tick the box is merely tight. Only
    # rc=3 (the check could not run at all) is news, and only to the journal —
    # this is a rendering path, and a rendering failure must not manufacture a
    # box-health problem. The console shows a missing artifact as `unreadable`,
    # never `ok`, so the gap stays visible where it belongs.
    if [ "$emit_check_rc" -eq 3 ]; then
        echo "box_health: headroom console publish could not run (rc=3)" >&2
    fi
    unset emit_check_rc
fi

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
    publish_unalerted 0
    # ALERTED_NOW is empty here, so this clears EVERY standing page. That is
    # the clean-tick recovery path and the most common one there is
    # (alpha-engine-config-I8105).
    finalize_alert_lifecycle
    emit_hygiene_envelope ""
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
    publish_unalerted 0
    finalize_alert_lifecycle
    emit_hygiene_envelope ""
    exit 0
fi

# Log the confirmed set so a firing is diagnosable from the journal directly
# (no S3 dedup-marker archaeology needed).
printf 'box_health: confirmed problems after %d samples:\n%s\n' "$attempt" "$confirmed" >&2

# ── Severity split — THREE tiers ────────────────────────────────────────
#
# Severity is a property of the invariant breached, not of the check that
# emitted it — overseer-policy.md invariant 17.
#
# HISTORY, because both previous shapes were wrong in opposite directions.
# Before 2026-07-29 every problem published at `warning`, which krepis.alerts
# delivers silently: a service being DOWN was as quiet as a censored memory
# reading. That was fixed by splitting off `notice: ` lines at `info` and
# publishing EVERYTHING ELSE at `error`, which pushes. That fixed direction one
# and re-broke direction two — the box now pages for conditions where nothing is
# degraded. Measured over 2026-07-27..08-03: box-health accounted for 96 of ~138
# fleet alert publishes, ~70%, essentially all of it pushing, and the largest
# single contributor was `memory budget: BREACH` firing while the box sat at 39%
# of RAM against a 60% limit with 2.3 GB free.
#
# Two tiers cannot express that, because "the box is unhealthy" and "a declared
# invariant about the box has drifted" are different invariants:
#
#   critical  A product or the box is degraded RIGHT NOW, or a service cannot
#             come back if it restarts. PUSHES a phone notification.
#   warning   A declared invariant is breached, or our ability to observe one is
#             impaired, while nothing is currently degraded. Silent in-channel;
#             still published to SNS and still emitted onto the Overseer intake
#             bus, where box-health is a declared alert class with
#             `intake: bus` / `response: drain-queue`. This tier is DELEGATED,
#             not discarded.
#   info      Hygiene about the monitoring itself. Silent, once a day.
#
# WHY THE DEFAULT IS `critical` AND NOT `warning`. classify_problem_severity
# below is an allow-list of things permitted to be quiet. A problem line that
# matches nothing falls through to critical and pages. That direction is
# deliberate: a new check added without a tiering decision must fail loud, never
# inherit silence. test_box_health_severity_tiers.py asserts the classification
# is TOTAL over every problem string this file emits, so the fall-through is a
# backstop against a check added in a hurry, not the normal path.
#
# WHAT THIS DEPENDS ON, stated because it is the risk the change creates: the
# `warning` tier reaches Brian only through email and reaches the plane only
# through the drain. A drain outage therefore converts this tier from "delegated"
# to "unattended". That coupling is real and is why box-health's own
# `watchdog:` coverage lines stay in this tier rather than dropping to info.
classify_problem_severity() {
    case "$1" in
        # ── info: hygiene about the MONITORING, substantive check still runs ──
        "notice: "*) echo info ;;

        # ── warning: a declared invariant drifted; nothing is degraded now ────
        # T1-8 IS CONSOLE-ONLY (alpha-engine-config-I7858, Brian ruling
        # 2026-08-21: "if i'm 4x away from the wall then i certainly no longer
        # want to be alerted of it").
        #
        # THE TIER WAS TRACKING THE WRONG INVARIANT. shared-application-host-
        # policy draws a distinction this classifier never did: T1-8
        # (sum(anon+swap) <= 50% of RAM) is a §5 HEADROOM invariant whose remedy
        # is "lower a cap, free memory, or move a service"; E3 (sustained
        # MemAvailable < 250 MB) is the §6 EXIT TRIGGER whose remedy is a resize
        # or a split. The policy says in as many words that the two "can
        # disagree in both directions and routinely will". This line put the
        # first one on the same channel as the second.
        #
        # Measured 2026-08-21 while the breach stood: MemAvailable 1128 MB
        # against E3's 250 MB threshold -- 4.5x away -- with zero kernel OOM
        # kills and `memory.pressure full avg300=0.00`. The finding was true,
        # standing, already ruled on (alpha-engine-config-I7804), and arrived in
        # the channel for 90 of 274 watchdog runs over fourteen days. That is
        # the single largest source of box-health traffic Brian sees.
        #
        # `info`, NOT deleted, and the difference is the whole design: the info
        # tier does not publish to krepis.alerts, but emit_hygiene_envelope
        # renders it on the console on EVERY run including clean ones, with the
        # finding's age. principles.md §7 -- a component emitting nothing is
        # unobserved, not healthy -- so the breach stays continuously visible
        # where a standing condition belongs, and stops arriving where a
        # changing one does.
        #
        # WHAT STAYS LOUD, deliberately, because this is the line someone will
        # later read as "memory alerts were turned off":
        #   "low memory: "*        -> critical (that IS E3's condition)
        #   "memory pressure: "*   -> critical (stalled on reclaim)
        #   OOMKills               -> its own CloudWatch alarm, untouched
        #   mem-available-crit/warn -> their own CloudWatch alarms, untouched
        # Nothing that indicates the box is actually running out of memory was
        # moved. What moved is the declared bound about how much headroom we
        # said we wanted.
        "memory budget: BREACH"*) echo info ;;
        "cgroup throttle: "*) echo warning ;;
        "disk high: "*) echo warning ;;
        "timer has not run in "*) echo warning ;;
        "timer enabled but not active"*) echo warning ;;
        "timer will never fire again: "*) echo warning ;;
        "service running but NOT enabled: "*) echo warning ;;
        # Coverage blindness. Serious — detection blindness outranks the defects
        # it hides — but it is a statement about the WATCHDOG, and
        # overseer-policy.md section 3 is explicit that the watchdog's findings
        # about its own coverage are "recorded and swept, never paged".
        "watchdog: "*) echo warning ;;

        # ── critical: degraded now, or cannot recover ─────────────────────────
        # Everything below is listed rather than left to the default so the
        # intent is on the record and the totality test can see it.
        "service down: "*) echo critical ;;
        "service not answering HTTP"*) echo critical ;;
        "port not listening: "*) echo critical ;;
        "low memory: "*) echo critical ;;
        "disk critical: "*) echo critical ;;
        "memory pressure: "*) echo critical ;;
        "timer job failing: "*) echo critical ;;
        # Latent, not current — but the unit cannot start again, and
        # reboot-if-needed.timer makes that an unattended, all-at-once event.
        "unit cannot restart: "*) echo critical ;;
        # alpha-engine-config-I7858: this check's whole purpose is to be an
        # independent backstop on the Overseer alert-drain — it must page,
        # not join a tier whose delivery depends on the same drain.
        "alert-drain not consuming: "*) echo critical ;;

        *) echo critical ;;
    esac
}

criticals=""; warnings=""; notices=""
while IFS= read -r _line; do
    [ -z "$_line" ] && continue
    case "$(classify_problem_severity "$_line")" in
        info)     notices="${notices}${_line}"$'\n' ;;
        warning)  warnings="${warnings}${_line}"$'\n' ;;
        *)        criticals="${criticals}${_line}"$'\n' ;;
    esac
done <<< "$confirmed"

# Strip the trailing newline each accumulator carries. NOT cosmetic: publish_problems
# does `mapfile -t _problems <<< "$lines"`, and `<<<` appends its own newline, so a
# value ending in "\n" yields a final EMPTY element — which renders as a bare " - "
# bullet in the message and, worse, lands in the dedup key. The previous shape got
# this for free because `$( ... )` strips trailing newlines; building the strings in
# the shell does not, and the difference is invisible until an alert fires.
criticals="${criticals%$'\n'}"
warnings="${warnings%$'\n'}"
notices="${notices%$'\n'}"

# The verdict metric counts problems that mean the BOX IS UNHEALTHY — the
# critical tier only. A warning is a statement about a declared bound, not
# evidence the box is degraded, and folding it in here is what made the gauge
# track bookkeeping. Neither lower tier is dropped from view: each has its own
# delivery below, and every tier reaches the Overseer bus via krepis.alerts.
publish_verdict "$(printf '%s' "$criticals" | grep -c . || true)"

# Coverage as a NUMBER, not N lines of prose (config#6657 deliverable 3): how
# many installed timers currently lack a `timers:` dead-man row. The per-timer
# `notice:` lines still name each one; this makes the gap a plottable series
# whose zero is emitted too — a timer added to the box without a row moves a
# graph instead of only appending prose nobody reads. Swallowed failure mode:
# same as the other metric publishes — journal line plus the alarm's
# missing-data breach if it persists.
aws cloudwatch put-metric-data --namespace "AlphaEngine/Box" \
    --metric-data \
    "MetricName=timers_without_deadman,Dimensions=[{Name=InstanceId,Value=${INSTANCE_ID}}],Value=$(printf '%s' "$notices" | grep -c 'timer has no dead-man threshold' || true),Unit=Count" \
    2>&1 | head -1 | sed 's/^/box_health: timer-coverage publish failed: /' >&2 || true

# publish_problems SEVERITY DEDUP_MIN PREFIX LINES
# One path for both tiers so they cannot drift apart in formatting, dedup
# behaviour, or failure reporting.
publish_problems() {
    local severity="$1" dedup_min="$2" prefix="$3" lines="$4" dkey_override="${5:-}"
    [ -z "$lines" ] && return 0
    local msg dkey p
    mapfile -t _problems <<< "$lines"
    msg="dashboard EC2 (${INSTANCE_ID}) ${prefix}:"
    for p in "${_problems[@]}"; do msg="$msg"$'\n'" - $p"; done
    if [ -n "$dkey_override" ]; then
        # A caller with a stable, non-textual identity for this finding (the
        # timer-job-failing dedup, alpha-engine-config-I7677) -- used verbatim
        # rather than folded into the set-derived key below.
        dkey="$dkey_override"
    else
        # dedup key derived from the problem set, so the same ongoing issue
        # alerts once per window rather than every 10 min. Namespaced by
        # severity so a notice cannot suppress an alert that happens to share
        # its text.
        dkey="boxhealth-${severity}-$(printf '%s' "${_problems[*]}" | tr ' /' '__' | cut -c1-64)"
    fi
    # krepis.alerts is the canonical CLI (config#1649): nousergon_lib.alerts is a
    # re-export shim since lib v0.66.0 — guard-less under `python -m` on 0.81.0
    # (silent exit-0 no-op, the config#1646 class). Invoke the real module.
    # A FAILED CRITICAL PUBLISH IS THE ONE THING THE CLOUDWATCH BACKSTOP EXISTS
    # FOR (alpha-engine-config-I8035), so it is counted here rather than only
    # written to the journal. Counted in LINES, not in calls: one failed call
    # can carry several findings, and the metric answers "how many confirmed
    # criticals did nobody get told about", not "how many publishes failed".
    # Lower tiers are journal-only as before — a warning that fails to publish
    # is still on the console via emit_hygiene_envelope.
    #
    # LIFECYCLE (alpha-engine-config-I8105). `--state` says whether this page
    # opens the condition or repeats one the previous tick already carried;
    # `--identity-key` is what the later clear will reference. The identity is
    # the dedup key deliberately: krepis treats identity_key as correlation
    # ONLY and never feeds it back into the dedup check, so the clear can name
    # a page whose dedup marker is still live without suppressing itself.
    local _state
    _state=$(alerted_state_lifecycle "$dkey")
    # Empty-safe expansion: this script is asserted to run under macOS bash 3.2,
    # where a bare "${arr[@]}" on an empty array is an unbound-variable error
    # under `set -u`.
    local _lifecycle=()
    if krepis_publish_lifecycle_args; then
        _lifecycle=(--state "$_state" --identity-key "$dkey")
    fi
    if ! "$ALERT_PY" -m krepis.alerts publish \
        --message "$msg" \
        --severity "$severity" \
        --source box-health \
        --dedup-key "$dkey" \
        --dedup-window-min "$dedup_min" \
        ${_lifecycle[@]+"${_lifecycle[@]}"}; then
        echo "box_health: $severity publish failed" >&2
        if [ "$severity" = "critical" ]; then
            UNALERTED_CRITICALS=$((UNALERTED_CRITICALS + ${#_problems[@]}))
        fi
    fi
    # Recorded whatever the publish outcome was: this file remembers which
    # CONDITIONS are standing, not which messages were delivered. Recording
    # only on success would mean a condition whose page failed could never
    # emit a clear either — one dropped signal turned into two. Delivery
    # failure has its own series (health_problems_unalerted).
    for p in "${_problems[@]}"; do
        [ -n "$p" ] || continue
        ALERTED_NOW="${ALERTED_NOW}${dkey}"$'\t'"${severity}"$'\t'"${p}"$'\n'
    done
}

# timer-job-failing findings get their OWN identity-keyed publish, one per
# unit, instead of sharing the general critical tier's set-derived
# text+cooldown dedup (alpha-engine-config-I7677 items 2-3). See
# timer_failure_dedup_key's docstring for why: a Result LEVEL that stays
# `exit-code` for a whole weekly cadence must page once for that run, not once
# per cooldown window for up to 7 days.
#
# The window (43200min = 30d) is a backstop against a stuck alerts store, not
# the actual dedup mechanism -- the KEY changing when InactiveExitTimestamp
# advances is what clears the page, so no window shorter than the longest
# timer cadence on this box (currently ~7d, see router-degraded-mode-drill,
# morning-signal-bakeoff, reboot-if-needed, box-hygiene, fstrim) could ever be
# both short enough to re-arm promptly and long enough not to re-page a stale
# already-fixed run in between.
#
# On-demand re-verification (item 3, DECIDED): re-running the failing unit by
# hand -- `systemctl start <unit>` -- is the repair/reverify path. Success
# advances InactiveExitTimestamp and Result, which rolls the key and clears
# the finding on the box's own next tick; a repeat failure correctly produces
# a NEW key and a NEW page. Chosen over degrading to `warning` once
# (age > 1 box-health cycle AND a newer deployed ExecStart target exists):
# that alternative needs a freshness comparison against the unit's ExecStart
# target through this box's multiple path-indirection layers (see the
# ALERT_PY two-candidate-path resolution near the top of this file for how
# fragile that kind of comparison already is here) for a benefit
# `systemctl start` already delivers directly and unambiguously.
timer_criticals=""; other_criticals=""
while IFS= read -r _line; do
    [ -z "$_line" ] && continue
    case "$_line" in
        "timer job failing: "*) timer_criticals="${timer_criticals}${_line}"$'\n' ;;
        *)                      other_criticals="${other_criticals}${_line}"$'\n' ;;
    esac
done <<< "$criticals"
timer_criticals="${timer_criticals%$'\n'}"
other_criticals="${other_criticals%$'\n'}"

while IFS= read -r _tf_line; do
    [ -z "$_tf_line" ] && continue
    _tf_unit="${_tf_line#timer job failing: }"
    _tf_unit="${_tf_unit%% (*}"
    _tf_svc=""
    [ -n "$_tf_unit" ] && _tf_svc=$(systemctl show "$_tf_unit" -p Unit --value 2>/dev/null)
    _tf_result=""; _tf_ts=""
    if [ -n "$_tf_svc" ]; then
        _tf_result=$(systemctl show "$_tf_svc" -p Result --value 2>/dev/null)
        _tf_ts=$(systemctl show "$_tf_svc" -p InactiveExitTimestamp --value 2>/dev/null)
    fi
    publish_problems critical 43200 "health alert" "$_tf_line" \
        "$(timer_failure_dedup_key "$_tf_unit" "$_tf_result" "$_tf_ts")"
done <<< "$timer_criticals"

criticals="$other_criticals"

# `critical`, not `error`. Both push identically today (krepis.alerts'
# SEVERITY_PUSH is {error, critical}), so this is not what makes the tier loud —
# the tier is loud because it is the only one left in that set. It is spelled
# `critical` so the rendered `[CRITICAL]` tag matches what the tier now means,
# and so a future narrowing of SEVERITY_PUSH to `critical` alone does not
# silently disarm this line.
publish_problems critical 60   "health alert" "$criticals"
# Silent in-channel, and deliberately so. Still SNS, still on the Overseer intake
# bus as alert class `box-health` (intake: bus, response: drain-queue).
#
# DAILY, NOT HOURLY (alpha-engine-config-I7822). This window was 60 minutes,
# matching critical, on the reasoning that "the tier changes who is woken, never
# how often the finding is recorded". Measured 2026-08-20: the standing
# `memory budget: BREACH` produced 24 notifications in 24 hours for one
# unchanged condition with an open decision on it (#7804), and Brian asked why
# he was still being dinged. A finding this tier itself labels "(no action
# urgent)" has no reader at an hourly cadence -- re-notification is only
# informative if something CHANGED.
#
# This suppresses repetition, never a new finding, and the mechanism is the
# dedup KEY rather than the window: `publish_problems` derives it from the
# problem SET, so a warning appearing, clearing, or changing text yields a
# different key and pages immediately regardless of this number. What the
# window governs is exactly one thing -- how often an UNCHANGED set is
# repeated. 1440 matches the `info` tier below, which already carries the
# identical "(no action urgent)" label and was already daily; the two tiers
# making the same promise at different cadences was the inconsistency.
#
# Criticals stay at 60. A degraded-now condition is worth repeating hourly
# precisely because it is not standing.
# 43200min = 30 days, reusing the backstop interval the timer-job-failing
# publish above already uses. Was 1440 (alpha-engine-config-I7822), which was
# itself down from 60.
#
# WHY DAILY WAS STILL WRONG. The tier is "silent", but silent here means
# krepis.alerts passing disable_notification=True, which suppresses the phone
# push and NOT the message — so a daily unchanged warning is a daily VISIBLE
# message about a condition that has a ruling on it (#7804). Measured
# 2026-08-20: the box oscillates tick-to-tick between rc=1 (T1-8 working-set
# breach, this tier) and rc=2 (hygiene, the tier below), so this fires most
# days for one already-decided fact.
#
# NOT SUPPRESSION, and the mechanism is the KEY not the window: publish_problems
# derives the dedup key from the problem SET, so a warning appearing, clearing
# or changing its text yields a different key and pages immediately whatever
# this number is. The window governs exactly one thing — how often an UNCHANGED
# set repeats. The console row (emit_hygiene_envelope below) is what makes that
# safe: the standing set is visible there continuously with each finding's age,
# rather than having to be remembered between repeats.
#
# WHY THIS TIER IS NOT SIMPLY MOVED OFF THE CHANNEL like the info tier was. Its
# whole claim to being quiet is that it is DELEGATED, reaching the Overseer
# intake bus as alert class box-health (intake: bus, response: drain-queue).
# Measured live 2026-08-20: all four alpha-engine-alert-drain-{0400,1000,1600,
# 2200}utc schedules are DISABLED under the 2026-08-07 automation pause
# (alpha-engine-config-I6984). The delegated consumer is not running on a
# schedule, so removing this tier from the channel would leave it with no reader
# at all — dressed as consistency with the notice change. Re-examine when the
# drain is unpaused: alpha-engine-config-I7858.
publish_problems warning  43200 "budget/coverage finding (no action urgent)" "$warnings"

# LAST publish_problems above, so ALERTED_NOW is final here: every page this
# run carried is recorded, and every key that was carried last run and is not
# carried now gets its terminator. Before publish_unalerted below only because
# a failed clear is its own series, not a failed critical.
ALERTED_NOW="${ALERTED_NOW%$'\n'}"
finalize_alert_lifecycle

# The info tier does NOT publish to krepis.alerts. It goes to the console.
#
# WHY, and why the two previous fixes did not work. The whole three-tier design
# rested on `info` being invisible to the operator. It is not.
# krepis/alerts.py sets SEVERITY_PUSH = {error, critical} and passes
# disable_notification=True for everything else -- and Telegram's
# disable_notification suppresses the PHONE PUSH, not the message. The message
# still lands in the chat, and SNS delivery is identical at every severity. So
# there was never a tier that kept a finding out of Brian's channel, only one
# that arrived without a buzz.
#
# That is why this has been "fixed" twice without the alerts stopping:
# 2026-07-29 split the tiers, 2026-08-20 (#7822) lowered the warning window
# 60 -> 1440. Both tuned CADENCE and SEVERITY. Neither controls VISIBILITY,
# which was the actual complaint -- "why do I have to keep raising box health".
#
# Hygiene about the monitoring belongs on a board that shows how long it has
# been true, not in a stream that re-announces it (#7822 deliverable 3). See
# emit_box_health_hygiene.py for why this is routing and not suppression: the
# envelope publishes on EVERY run including clean ones, carries ran_at +
# cadence_minutes so the console marks it STALE if the emitter dies, and renders
# as `unreadable` -- never `ok` -- when the artifact is missing.
# Both lower tiers, one surface. `warning` appears here IN ADDITION to its
# channel publish above, never instead of it; `notice` appears here only.
emit_hygiene_envelope "$(printf '%s\n%s' "$notices" "$warnings" | grep -v '^$' || true)"

# LAST, deliberately: every critical publish above has now either landed or
# failed, so this is the only point at which UNALERTED_CRITICALS is final.
# Publishing it earlier would emit a count of failures that had not been
# attempted yet, which is the shape publish_verdict has and the reason this is
# a separate series rather than a second argument to it.
publish_unalerted "$UNALERTED_CRITICALS"
