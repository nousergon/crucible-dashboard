#!/bin/bash
# test_box_health_throttle_rate.sh — regression test for
# classify_throttle_delta() in box_health.sh (alpha-engine-config-I5216).
#
# The bug it guards: the cgroup throttle check alerted on
# `memory.events::high > 0`. That counter is MONOTONIC for the life of the
# cgroup — it resets only when the cgroup is recreated (service restart or
# reboot). Two consequences, both fatal to the signal:
#
#   1. One transient reclaim spike at 03:00 pages forever. The condition can
#      never clear on its own.
#   2. Fixing the memory cap DOES NOT clear it. The alert survives its own
#      remedy, so it cannot be used to confirm the remedy worked — which is
#      exactly what I5216's acceptance criteria ask of it.
#
# It also could not distinguish "throttled 7000 times in the last hour" from
# "throttled once three weeks ago". Only the delta between samples can.
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest:
#   bash infrastructure/test_box_health_throttle_rate.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_health.sh"

eval "$(awk '/^classify_throttle_delta\(\) \{/,/^\}/' "$TARGET_SCRIPT")"
declare -F classify_throttle_delta >/dev/null || {
    echo "FAIL - classify_throttle_delta() not found in box_health.sh"; exit 1; }

FLOOR=10   # must mirror CGROUP_HIGH_DELTA_MIN; asserted against the source below
FAILURES=0

assert_silent() {
    local desc="$1"; shift
    local out; out=$(classify_throttle_delta "svc.service" "$@")
    if [ -z "$out" ]; then echo "ok   - $desc"
    else echo "FAIL - $desc (expected silence, got: $out)"; FAILURES=$((FAILURES+1)); fi
}
assert_reports() {
    local desc="$1" want="$2"; shift 2
    local out; out=$(classify_throttle_delta "svc.service" "$@")
    if [ -z "$out" ]; then
        echo "FAIL - $desc (expected a report, got none)"; FAILURES=$((FAILURES+1))
    elif [[ "$out" != *"$want"* ]]; then
        echo "FAIL - $desc (expected '$want', got: $out)"; FAILURES=$((FAILURES+1))
    else echo "ok   - $desc"; fi
}

echo "== THE REGRESSION: a large lifetime total with no recent movement =="
# metron-api's real numbers. Under the old `> 0` rule this paged forever; it
# must now be silent, because nothing has throttled since the last check.
assert_silent "7347 lifetime events but zero delta -> silent" 7347 7347 "$FLOOR"
assert_silent "huge total, tiny delta below the floor -> silent" 7350 7347 "$FLOOR"

echo "== active throttling must still report =="
assert_reports "delta at the floor -> reports" "hit MemoryHigh 10x" 7357 7347 "$FLOOR"
# A startup burst: the whole 7347 arriving inside one tick is what an
# undersized cap looks like, and must report.
assert_reports "startup burst well above the floor" \
    "hit MemoryHigh 7347x" 7347 0 "$FLOOR"
assert_reports "moderate burst from a settled baseline" "hit MemoryHigh 500x" 7847 7347 "$FLOOR"

echo "== the fix must be able to CLEAR the alert =="
# The whole point: after raising the cap and restarting, the counter resets to
# a small number and stops moving. That must read as healthy.
assert_silent "post-remedy: counter reset and steady -> silent" 3 3 "$FLOOR"

echo "== first run has no baseline =="
# Reporting the lifetime total here would reintroduce the original defect.
assert_silent "no baseline yet -> silent, not the lifetime total" 7347 "" "$FLOOR"
assert_silent "garbage baseline -> silent" 7347 "n/a" "$FLOOR"

echo "== a service restart resets the cgroup counter =="
# Counter going backwards is a recreated cgroup, not negative throttling.
assert_silent "counter went backwards (service restarted) -> silent" 5 7347 "$FLOOR"

echo "== an unreadable counter is a watchdog malfunction, not silence =="
assert_reports "empty current counter is reported" "cannot read cgroup throttle counter" "" 100 "$FLOOR"
assert_reports "non-numeric current counter is reported" "cannot read cgroup throttle counter" "abc" 100 "$FLOOR"

echo "== the floor in this test matches the script =="
_src_floor=$(grep -E '^CGROUP_HIGH_DELTA_MIN=' "$TARGET_SCRIPT" | cut -d= -f2)
if [ "$_src_floor" = "$FLOOR" ]; then
    echo "ok   - CGROUP_HIGH_DELTA_MIN=$FLOOR matches box_health.sh"
else
    echo "FAIL - test floor $FLOOR != box_health.sh CGROUP_HIGH_DELTA_MIN=$_src_floor"
    FAILURES=$((FAILURES+1))
fi

echo "== the baseline must not be refreshed between confirmation samples =="
# If throttle_baseline_write ran inside snapshot_problems, samples 2..N would
# see a ~0 delta, the confirm-on-retry intersection would drop the line, and
# real throttling would be filtered out by the noise filter. It must be an
# EXIT trap instead.
if grep -q 'trap throttle_baseline_write EXIT' "$TARGET_SCRIPT" \
   && ! awk '/^snapshot_problems\(\) \{/,/^\}/' "$TARGET_SCRIPT" | grep -q 'throttle_baseline_write'; then
    echo "ok   - baseline written once per run via EXIT trap, not per sample"
else
    echo "FAIL - throttle_baseline_write must run once per run (EXIT trap), never inside snapshot_problems"
    FAILURES=$((FAILURES+1))
fi

echo "== the state directory must be declared in the unit, not mkdir'd at runtime =="
# box-health.service runs as User=ec2-user, which cannot create a directory
# under root-owned /var/lib. The first shipped version relied on `mkdir -p` and
# so never wrote a baseline — and "no baseline" is this check's HEALTHY case,
# so it was silently dead. Verified live 2026-07-28.
_unit="$SCRIPT_DIR/systemd/box-health.service"
if grep -q '^StateDirectory=box-health' "$_unit"; then
    echo "ok   - box-health.service declares StateDirectory=box-health"
else
    echo "FAIL - box-health.service must declare StateDirectory=box-health; a"
    echo "       runtime mkdir under /var/lib fails as ec2-user and the check dies silently"
    FAILURES=$((FAILURES+1))
fi
if grep -q 'THROTTLE_STATE_DIR="\${STATE_DIRECTORY:-' "$TARGET_SCRIPT"; then
    echo "ok   - box_health.sh honours \$STATE_DIRECTORY"
else
    echo "FAIL - box_health.sh must use \$STATE_DIRECTORY (systemd-provided) with a fallback"
    FAILURES=$((FAILURES+1))
fi
if grep -q 'cgroup throttling is UNMONITORED' "$TARGET_SCRIPT"; then
    echo "ok   - an unwritable state dir is reported, not silently tolerated"
else
    echo "FAIL - an unwritable state dir must be REPORTED: no baseline is the"
    echo "       healthy case, so silence there means the check is dead"
    FAILURES=$((FAILURES+1))
fi

echo
if [ "$FAILURES" -eq 0 ]; then echo "PASS - all classify_throttle_delta assertions"; exit 0; fi
echo "FAILED - $FAILURES assertion(s)"; exit 1
