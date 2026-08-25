#!/bin/bash
# test_box_health_timer_staleness.sh — regression test for
# classify_timer_staleness() in box_health.sh (alpha-engine-config-I5209).
#
# What it guards, and why it is separate from the scheduler-state check:
#
# classify_timer() answers "is this timer scheduled to fire?". It structurally
# CANNOT answer "did the job run, and did it work?" — a timer whose service
# exits non-zero on every fire, or whose OnCalendar was mis-edited to a
# far-future date, is `active`, `waiting`, with a perfectly valid next elapse.
# classify_timer calls that healthy, correctly, because the scheduler IS
# healthy. metron-refresh's 2026-07-25 OOM kill (config-I4487) was caught by
# the scheduler check only by luck: it happened to also stop the timer.
#
# I4487 specified both halves — "flags any enabled timer whose NEXT is `-` OR
# whose LAST is older than 2x its interval". Only the first shipped. This is
# the second.
#
# Every assertion below is verified to FAIL against the un-fixed code, not
# merely to pass against the fixed code — see the harness note in
# test_box_health_timer_deadman.sh for why that distinction is load-bearing.
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest (which is
# what makes it run in CI at all):
#   bash infrastructure/test_box_health_timer_staleness.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_health.sh"

if [ ! -r "$TARGET_SCRIPT" ]; then
    echo "FAIL - cannot read $TARGET_SCRIPT"
    exit 1
fi

# Source ONLY the two pure functions under test. box_health.sh at top level
# loads env, hits IMDS and reads the manifest, none of which belongs here.
eval "$(awk '/^human_age\(\) \{/,/^\}/' "$TARGET_SCRIPT")"
eval "$(awk '/^classify_timer_staleness\(\) \{/,/^\}/' "$TARGET_SCRIPT")"
eval "$(awk '/^timer_failure_dedup_key\(\) \{/,/^\}/' "$TARGET_SCRIPT")"

for fn in human_age classify_timer_staleness timer_failure_dedup_key; do
    if ! declare -F "$fn" >/dev/null; then
        echo "FAIL - $fn() not found in box_health.sh (extraction failed)"
        exit 1
    fi
done

FAILURES=0
NOW=1753722215          # fixed epoch; nothing here may depend on wall clock

# assert_healthy DESC LAST BUDGET RESULT
assert_healthy() {
    local desc="$1"; shift
    local out
    out=$(classify_timer_staleness "unit.timer" "$NOW" "$@")
    if [ -z "$out" ]; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc (expected no problem, got: $out)"
        FAILURES=$((FAILURES + 1))
    fi
}

# assert_problem DESC EXPECTED_SUBSTRING LAST BUDGET RESULT
assert_problem() {
    local desc="$1" want="$2"; shift 2
    local out
    out=$(classify_timer_staleness "unit.timer" "$NOW" "$@")
    if [ -z "$out" ]; then
        echo "FAIL - $desc (expected a problem, got none)"
        FAILURES=$((FAILURES + 1))
    elif [[ "$out" != *"$want"* ]]; then
        echo "FAIL - $desc (expected substring '$want', got: $out)"
        FAILURES=$((FAILURES + 1))
    else
        echo "ok   - $desc"
    fi
}

echo "== healthy jobs must NOT page =="
assert_healthy "ran 5m ago against a 30m budget" \
    "$((NOW - 300))" 1800 success
assert_healthy "ran exactly at the budget boundary (not yet over)" \
    "$((NOW - 1800))" 1800 success
assert_healthy "never triggered — no baseline, classify_timer owns this case" \
    "" 691200 success
assert_healthy "never triggered AND no result yet" \
    "" 691200 ""

echo "== a job that FIRES ON TIME but FAILS must page =="
# THE WHOLE POINT. Fresh trigger, valid schedule, non-zero exit. Invisible to
# every scheduler-state property; this is the only check that sees it.
assert_problem "fresh trigger but last run failed" \
    "timer job failing" \
    "$((NOW - 60))" 1800 failed
assert_problem "OOM-killed run (the metron-refresh 2026-07-25 signature)" \
    "timer job failing" \
    "$((NOW - 60))" 1800 oom-kill
assert_problem "timeout result" \
    "timer job failing" \
    "$((NOW - 60))" 1800 timeout

echo "== a job that has silently stopped running must page =="
assert_problem "31h since last run against a 26h budget" \
    "has not run in 31h" \
    "$((NOW - 111600))" 93600 success
assert_problem "one second past the budget" \
    "has not run" \
    "$((NOW - 1801))" 1800 success

echo "== coverage self-check =="
assert_problem "enabled timer with no declared budget is NAMED, not skipped" \
    "no dead-man threshold" \
    "$((NOW - 60))" "" success

echo "== the coverage hole must not suppress the check it is a hole in =="
# THE 2026-07-29 DEFECT. The no-budget branch returned EARLY, above the
# execution-outcome check. So a timer installed without a `timers:` row -- the
# exact state every brand-new timer is in -- had its outcome check skipped
# entirely, and the watchdog reported the missing row instead of the failure.
# Live: metron-intraday.timer had failed 48 of 48 runs on an S3 AccessDenied and
# box_health.sh said only "add a timers: row to budget.yaml". A guard filtering
# out the class it exists to protect, again.
#
# The outcome check needs NOTHING from budget.yaml, so it must run first and
# unconditionally. Both facts must be reported, not one.
out=$(classify_timer_staleness "metron-intraday.timer" "$NOW" "$((NOW - 60))" "" failed)
if [[ "$out" == *"timer job failing"* ]] && [[ "$out" == *"no dead-man threshold"* ]]; then
    echo "ok   - a FAILING timer with no budget row reports the failure, not just the gap"
else
    echo "FAIL - expected both the job-failure line and the coverage line, got: $out"
    FAILURES=$((FAILURES + 1))
fi

# Ordering matters for the human reading the alert: the outage comes first.
if [[ "${out%%$'\n'*}" == *"timer job failing"* ]]; then
    echo "ok   - the job-failure line is reported before the coverage line"
else
    echo "FAIL - the job-failure line must come FIRST, got: $out"
    FAILURES=$((FAILURES + 1))
fi

echo "== the coverage line is notice-class, the failure line is not =="
# Severity is a property of the invariant breached, not of the check that
# emitted it (overseer-policy invariant 17). box_health.sh partitions on the
# `notice: ` prefix: prefixed lines go out at info, everything else pushes.
# A missing budget row must never push; a failing job always must.
if [[ "$(classify_timer_staleness "u.timer" "$NOW" "$((NOW - 60))" "" success)" == notice:* ]]; then
    echo "ok   - the missing-row line carries the notice: prefix"
else
    echo "FAIL - the missing-row line must be notice-class or it pages"
    FAILURES=$((FAILURES + 1))
fi
if [[ "$(classify_timer_staleness "u.timer" "$NOW" "$((NOW - 60))" 1800 failed)" == notice:* ]]; then
    echo "FAIL - a failing job must NOT be notice-class"
    FAILURES=$((FAILURES + 1))
else
    echo "ok   - a failing job stays alert-class"
fi

echo "== malfunction cases are distinct from job failures =="
assert_problem "last trigger in the future (clock skew) is not silently passed" \
    "clock skew" \
    "$((NOW + 600))" 1800 success

echo "== a non-numeric last-trigger must never reach the arithmetic =="
# THE LIVE-ONLY BUG. `systemctl show --timestamp=unix` silently does not apply
# to LastTriggerUSec, so the raw string "Tue 2026-07-28 17:03:35 UTC" reached
# $(( )). Under `set -u` bash evaluates bare words there as variable names and
# aborted the ENTIRE snapshot with "Tue: unbound variable" — every other check
# on the box went down with it. Synthetic-epoch assertions could not see this;
# only running against real systemd did.
assert_problem "raw systemd timestamp is reported, not evaluated" \
    "cannot parse timer last-trigger" \
    "Tue 2026-07-28 17:03:35 UTC" 1800 success
assert_problem "partially-numeric garbage is also rejected" \
    "cannot parse timer last-trigger" \
    "1785258215x" 1800 success

echo "== failing AND stale reports both facts =="
out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 111600))" 93600 failed)
lines=$(printf '%s\n' "$out" | grep -c .)
if [ "$lines" -eq 2 ] && [[ "$out" == *"job failing"* ]] && [[ "$out" == *"has not run"* ]]; then
    echo "ok   - a job both failing and stale reports both, not one"
else
    echo "FAIL - expected both a failure line and a staleness line, got: $out"
    FAILURES=$((FAILURES + 1))
fi

echo "== human_age renders alert-readable ages =="
# 26h/31h must NOT both collapse to "1d" — an alert reading
# "has not run in 1d (budget 1d)" reads as healthy. Days start at 48h.
for pair in "45:45s" "300:5m" "5400:1h" "93600:26h" "111600:31h" "172800:2d" "691200:8d"; do
    got=$(human_age "${pair%%:*}")
    if [ "$got" = "${pair##*:}" ]; then
        echo "ok   - human_age ${pair%%:*} -> $got"
    else
        echo "FAIL - human_age ${pair%%:*} -> $got (expected ${pair##*:})"
        FAILURES=$((FAILURES + 1))
    fi
done

echo "== the message carries the failing run's OWN timestamp and next elapse (alpha-engine-config-I7677) =="
assert_problem "failing run carries its own timestamp, not a relative age" \
    "failing run started Tue 2026-08-18 10:30:52 UTC" \
    "$((NOW - 60))" 1800 failed "Tue 2026-08-18 10:30:52 UTC" "Tue 2026-08-25 10:30:00 UTC"
assert_problem "failing run carries the timer's next scheduled attempt" \
    "next attempt Tue 2026-08-25 10:30:00 UTC" \
    "$((NOW - 60))" 1800 failed "Tue 2026-08-18 10:30:52 UTC" "Tue 2026-08-25 10:30:00 UTC"

echo "== the message is IDENTICAL across ticks for the SAME failing run =="
# This is what makes a repeat page recognisable as the same run, not a new
# one -- the text must not depend on 'now' (a computed relative age would
# change every 10-min tick and defeat identity-keyed dedup downstream).
out1=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" 1800 failed \
    "Tue 2026-08-18 10:30:52 UTC" "Tue 2026-08-25 10:30:00 UTC")
out2=$(classify_timer_staleness "unit.timer" "$((NOW + 21600))" "$((NOW - 60))" 1800 failed \
    "Tue 2026-08-18 10:30:52 UTC" "Tue 2026-08-25 10:30:00 UTC")
# Compare the FAILING line only. Advancing 'now' by 6h legitimately adds a
# separate "has not run in ..." staleness line -- a different finding, whose
# whole job is to depend on now. The invariant under test is that the
# job-failing line itself does not.
fail1=$(printf '%s\n' "$out1" | grep '^timer job failing: ')
fail2=$(printf '%s\n' "$out2" | grep '^timer job failing: ')
if [ -n "$fail1" ] && [ "$fail1" = "$fail2" ]; then
    echo "ok   - message text is stable across ticks (now advanced 6h)"
else
    echo "FAIL - message changed with 'now' alone: [$fail1] vs [$fail2]"
    FAILURES=$((FAILURES + 1))
fi

echo "== missing failing-run detail degrades gracefully (backward-compatible 5-arg call) =="
assert_problem "5-arg call (no timestamp detail) still reports the failure" \
    "timer job failing: unit.timer (last run result=failed)" \
    "$((NOW - 60))" 1800 failed

echo "== timer_failure_dedup_key is stable per run and changes when the run changes =="
k1=$(timer_failure_dedup_key "router-degraded-mode-drill.timer" "exit-code" "Tue 2026-08-18 10:30:52 UTC")
k2=$(timer_failure_dedup_key "router-degraded-mode-drill.timer" "exit-code" "Tue 2026-08-18 10:30:52 UTC")
if [ "$k1" = "$k2" ]; then
    echo "ok   - same (unit, result, timestamp) produces the same key"
else
    echo "FAIL - key is not stable for identical inputs: [$k1] vs [$k2]"
    FAILURES=$((FAILURES + 1))
fi
k3=$(timer_failure_dedup_key "router-degraded-mode-drill.timer" "success" "Tue 2026-08-25 10:30:04 UTC")
if [ "$k1" != "$k3" ]; then
    echo "ok   - a new run (new Result, new timestamp) produces a DIFFERENT key"
else
    echo "FAIL - key did not change when the run changed: [$k1]"
    FAILURES=$((FAILURES + 1))
fi
k4=$(timer_failure_dedup_key "other-unit.timer" "exit-code" "Tue 2026-08-18 10:30:52 UTC")
if [ "$k1" != "$k4" ]; then
    echo "ok   - different units never share a key"
else
    echo "FAIL - two different units produced the same key: [$k1]"
    FAILURES=$((FAILURES + 1))
fi

# ── A RUN IN FLIGHT MUST NOT READ AS RECOVERY (alpha-engine-config-I8359) ────
#
# systemd resets `Result` to `success` when a unit STARTS. Measured on
# i-09b539c844515d549:
#   BEFORE: Result=success ActiveState=inactive   SubState=dead
#   DURING: Result=success ActiveState=activating SubState=start
# so a timer failing for days reads healthy for the whole of its next attempt.
# All four confirm-on-retry samples fall inside that run, so the retry window
# confirms the finding's ABSENCE instead of catching the flap.
#
# Live consequence, 2026-08-25: health_problems_unalerted went
# 2.0, 2.0, 0.0, 2.0, 2.0 and the CloudWatch backstop emailed OK at 18:23:27
# UTC for a condition that never ended. With the krepis pin restored (I8105)
# that flap becomes a delivered "resolved" page, which is why it is corrected.
echo
echo "== mid-run: Result is not yet meaningful =="

PRIOR="timer job failing: unit.timer (last run result=exit-code, failing run started Mon 2026-08-24 07:01:39 UTC)"

# args after the unit+now pair: last, budget, result, fail_since, next, active, prior
out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" "" "success" "" "" "activating" "$PRIOR")
if [ "$out" = "$PRIOR" ]; then
    echo "ok   - a failing unit mid-run carries its PRIOR finding verbatim"
else
    echo "FAIL - mid-run did not carry the prior finding: [$out]"
    FAILURES=$((FAILURES + 1))
fi

# Byte-identical matters: the identity key is derived from this line's finding,
# and any drift would roll the key, which IS a clear plus a new page.
k_prior=$(timer_failure_dedup_key "unit.timer" "exit-code" "Mon 2026-08-24 07:01:39 UTC")
k_carry=$(timer_failure_dedup_key "unit.timer" "exit-code" "Mon 2026-08-24 07:01:39 UTC")
if [ "$k_prior" = "$k_carry" ]; then
    echo "ok   - the carried finding keeps the identity key stable"
else
    echo "FAIL - carried finding rolled the key"
    FAILURES=$((FAILURES + 1))
fi

out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" "" "success" "" "" "active" "$PRIOR")
if [ "$out" = "$PRIOR" ]; then
    echo "ok   - ActiveState=active is treated as in-flight too"
else
    echo "FAIL - ActiveState=active did not carry: [$out]"
    FAILURES=$((FAILURES + 1))
fi

# A unit with no standing finding must NOT acquire one just for running.
out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" "" "success" "" "" "activating" "")
if [ -z "$out" ]; then
    echo "ok   - a healthy unit mid-run invents no finding"
else
    echo "FAIL - mid-run fabricated a finding: [$out]"
    FAILURES=$((FAILURES + 1))
fi

# Self-correcting: once the run FINISHES successfully, the finding is gone even
# though a prior line exists. This is the assertion that proves the carry is a
# hold and not a latch.
# Asserted on the FAILING line specifically, not on empty output: with no
# budget row this call also emits the dead-man coverage notice, which is a
# different finding and correct here.
out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" "" "success" "" "" "inactive" "$PRIOR")
if ! printf '%s' "$out" | grep -q "timer job failing"; then
    echo "ok   - a completed successful run clears, prior finding notwithstanding"
else
    echo "FAIL - the carry latched past completion: [$out]"
    FAILURES=$((FAILURES + 1))
fi

# And a completed FAILING run still reports, unchanged from before this fix.
out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" "" "exit-code" "" "" "inactive" "")
case "$out" in
    "timer job failing: unit.timer"*)
        echo "ok   - a completed failing run still reports" ;;
    *)
        echo "FAIL - completed failing run no longer reports: [$out]"
        FAILURES=$((FAILURES + 1)) ;;
esac

# Back-compat: existing callers pass 7 args. Absent ActiveState must behave
# exactly as before, or every other assertion in this file is testing a
# different function than the one that ships.
out=$(classify_timer_staleness "unit.timer" "$NOW" "$((NOW - 60))" "" "exit-code")
case "$out" in
    "timer job failing: unit.timer"*)
        echo "ok   - omitting ActiveState preserves the pre-I8359 behaviour" ;;
    *)
        echo "FAIL - 7-arg call changed behaviour: [$out]"
        FAILURES=$((FAILURES + 1)) ;;
esac

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "PASS - all classify_timer_staleness assertions"
    exit 0
fi
echo "FAILED - $FAILURES assertion(s)"
exit 1
