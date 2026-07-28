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

for fn in human_age classify_timer_staleness; do
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

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "PASS - all classify_timer_staleness assertions"
    exit 0
fi
echo "FAILED - $FAILURES assertion(s)"
exit 1
