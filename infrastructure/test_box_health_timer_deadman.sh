#!/bin/bash
# test_box_health_timer_deadman.sh — regression test for classify_timer() in
# box_health.sh, the box's only dead-man monitor for timer-driven jobs
# (policy T0-4, alpha-engine-config-I4487).
#
# Root cause under test: the original predicate was "an `enabled` timer whose
# NEXT column in `systemctl list-timers` is `-` will never fire again."  That
# reads a THIRD state as dead. While a timer's own triggered service is
# executing, systemd deliberately does not compute a next-elapse — the timer
# sits at SubState=running with NextElapseUSecMonotonic=infinity, which
# list-timers renders as `-`, visually identical to a genuinely dead timer.
#
# box_health.sh runs INSIDE box-health.service, so box-health.timer was
# mid-trigger at every sample it ever took: 144 of 144 runs over 36h flagged
# the watchdog's own timer and paged hourly about a timer that was provably
# firing on schedule. The same race hits any timer whose job outlives the
# ~12s confirmation window, so it was never a box-health-only defect.
#
# classify_timer() is a pure function of its arguments precisely so this can
# be asserted without systemd. Property shapes below were captured live from
# i-09b539c844515d549 on 2026-07-28 (`systemctl show <timer> -p ...`), not
# invented — the calendar/monotonic asymmetry (Realtime empty vs Monotonic=0)
# is the reason neither property alone is a sufficient test.
#
# No pytest/Make harness exists for this repo's shell logic (tests/ is pytest
# for the Streamlit app), so this is a self-contained bash runner. It is
# invoked by tests/test_dash_deploy_infra.py so CI actually runs it, and can
# also be run directly:
#   bash infrastructure/test_box_health_timer_deadman.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_health.sh"

if [ ! -r "$TARGET_SCRIPT" ]; then
    echo "FAIL - cannot read $TARGET_SCRIPT"
    exit 1
fi

# Source ONLY the classify_timer function. box_health.sh at top level loads
# env, hits IMDS and reads the manifest, none of which belongs in a unit test.
eval "$(awk '/^classify_timer\(\) \{/,/^\}/' "$TARGET_SCRIPT")"

if ! declare -F classify_timer >/dev/null; then
    echo "FAIL - classify_timer() not found in box_health.sh (extraction failed)"
    exit 1
fi

FAILURES=0

# assert_healthy NAME DESC ACTIVE SUB NEXT_REAL NEXT_MONO
assert_healthy() {
    local desc="$1"; shift
    local out
    out=$(classify_timer "unit.timer" "$@")
    if [ -z "$out" ]; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc (expected no problem, got: $out)"
        FAILURES=$((FAILURES + 1))
    fi
}

# assert_problem DESC EXPECTED_SUBSTRING ACTIVE SUB NEXT_REAL NEXT_MONO
assert_problem() {
    local desc="$1" want="$2"; shift 2
    local out
    out=$(classify_timer "unit.timer" "$@")
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

echo "== healthy timers must NOT page =="

# THE REGRESSION. box-health.timer as observed from inside its own service.
# Under the old NEXT=='-' predicate this paged; it must now be silent.
assert_healthy "timer mid-trigger (its own service running) is healthy" \
    "active" "running" "" "infinity"

# Live shapes, captured 2026-07-28.
assert_healthy "monotonic timer waiting (box-health.timer, OnUnitActiveSec)" \
    "active" "waiting" "" "1d 2h 17min 22.510165s"
assert_healthy "calendar timer waiting (metron-refresh.timer, OnCalendar)" \
    "active" "waiting" "Tue 2026-07-28 20:45:00 UTC" "0"
assert_healthy "calendar timer that has never yet run (reboot-if-needed.timer)" \
    "active" "waiting" "Sun 2026-08-02 07:00:00 UTC" "0"

echo "== dead timers MUST page =="

# THE OUTAGE THIS CHECK EXISTS FOR: metron-refresh.timer after its 2026-07-25
# OOM kill — enabled, but inactive, so it never fires again until a reboot.
assert_problem "enabled but inactive (I4487 metron-refresh signature)" \
    "will not fire until reboot" \
    "inactive" "dead" "" "0"
assert_problem "failed timer unit" \
    "will not fire until reboot" \
    "failed" "failed" "" "0"

# Active and waiting, but with no next elapse of either kind.
assert_problem "waiting with no next elapse at all (monotonic infinity)" \
    "will never fire again" \
    "active" "waiting" "" "infinity"
assert_problem "waiting with no next elapse at all (monotonic zero)" \
    "will never fire again" \
    "active" "waiting" "" "0"

echo "== the two dead cases stay distinguishable =="
# They carry different remedies (systemctl start vs. investigate the unit) and
# feed box_health.sh's dedup key, so they must not collapse into one string.
inactive_msg=$(classify_timer "unit.timer" "inactive" "dead" "" "0")
noelapse_msg=$(classify_timer "unit.timer" "active" "waiting" "" "infinity")
if [ "$inactive_msg" = "$noelapse_msg" ]; then
    echo "FAIL - inactive and no-next-elapse must produce distinct messages"
    FAILURES=$((FAILURES + 1))
else
    echo "ok   - inactive and no-next-elapse produce distinct messages"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "PASS - all classify_timer assertions"
    exit 0
fi
echo "FAILED - $FAILURES assertion(s)"
exit 1
