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

# ── extracting a function body out of box_health.sh ─────────────────────────
#
# `awk '/^name\(\) \{/,/^\}/'` is the obvious form and is NOT portable: `\(` and
# `\{` are UNDEFINED escapes in POSIX ERE. gawk and BSD awk accept them as
# literals; mawk — the default `awk` on GitHub's Ubuntu runners — is entitled to
# do something else with `\{`, which begins an interval expression.
#
# This bit on 2026-08-12 (alpha-engine-config-I6972): the `call site must pass a
# stall reading` assertion failed on 2 of 4 CI runs of an unrelated PR while
# passing deterministically on macOS, on a check that reads a fixed file and can
# only be deterministic.
#
# `extract_fn` uses index/substr string comparison and no regex at all, so every
# awk implementation agrees. Callers MUST route an empty result to
# `harness_fault`, because the two conditions are opposite and the old code
# reported them identically: an extraction that yields nothing makes every
# `grep` inside it fail, so a broken harness announced itself as the very defect
# the assertion exists to catch — cry-wolf in the fail-closed-looking direction,
# which is how an assertion stops being read.
extract_fn() {
    local name="$1"
    awk -v open="${name}() {" '
        index($0, open) == 1 { inside = 1 }
        inside               { print }
        inside && $0 == "}"  { exit }
    ' "$TARGET_SCRIPT"
}

HARNESS_FAULTS=0
harness_fault() {
    echo "HARNESS - $*" >&2
    HARNESS_FAULTS=$((HARNESS_FAULTS+1))
}

_CLASSIFY_BODY="$(extract_fn classify_throttle_delta)"
[ -n "$_CLASSIFY_BODY" ] || {
    echo "HARNESS - could not extract classify_throttle_delta() from $TARGET_SCRIPT" >&2
    exit 2; }
eval "$_CLASSIFY_BODY"
declare -F classify_throttle_delta >/dev/null || {
    echo "FAIL - classify_throttle_delta() not found in box_health.sh"; exit 1; }

# Extracted ONCE, up front, so every assertion below shares one reading and a
# failure to extract is reported once as a harness fault rather than N times as
# N findings.
SNAPSHOT_BODY="$(extract_fn snapshot_problems)"
[ -n "$SNAPSHOT_BODY" ] || harness_fault \
    "could not extract snapshot_problems() from $TARGET_SCRIPT — the assertions about its call site cannot be evaluated"

FLOOR=10   # must mirror CGROUP_HIGH_DELTA_MIN; asserted against the source below
# The harm gate's threshold. The function reads it from the environment as a
# global, exactly as it does inside box_health.sh; asserted against the source
# at the bottom of this file so the two cannot drift.
CGROUP_STALL_MIN=1.0
STALLED=25.00     # a service genuinely stalled on reclaim (vires read 55 live)
QUIET=0.05        # background reclaim, no measurable cost (console read 0.05)
FAILURES=0

# Every assertion passes a stall reading. The 5th argument defaults to empty
# inside the function, and empty means "unreadable" — which REPORTS — so a test
# that omitted it would silently exercise the fail-open path instead of the one
# it names. stderr is dropped: the function journals the numbers there.
assert_silent() {
    local desc="$1"; shift
    local out; out=$(classify_throttle_delta "svc.service" "$@" 2>/dev/null)
    if [ -z "$out" ]; then echo "ok   - $desc"
    else echo "FAIL - $desc (expected silence, got: $out)"; FAILURES=$((FAILURES+1)); fi
}
assert_reports() {
    local desc="$1" want="$2"; shift 2
    local out; out=$(classify_throttle_delta "svc.service" "$@" 2>/dev/null)
    if [ -z "$out" ]; then
        echo "FAIL - $desc (expected a report, got none)"; FAILURES=$((FAILURES+1))
    elif [[ "$out" != *"$want"* ]]; then
        echo "FAIL - $desc (expected '$want', got: $out)"; FAILURES=$((FAILURES+1))
    else echo "ok   - $desc"; fi
}

echo "== THE REGRESSION: a large lifetime total with no recent movement =="
# metron-api's real numbers. Under the old `> 0` rule this paged forever; it
# must now be silent, because nothing has throttled since the last check.
assert_silent "7347 lifetime events but zero delta -> silent" 7347 7347 "$FLOOR" "$STALLED"
assert_silent "huge total, tiny delta below the floor -> silent" 7350 7347 "$FLOOR" "$STALLED"

echo "== active throttling WITH stall must still report =="
assert_reports "delta at the floor -> reports" "measurable reclaim stall" 7357 7347 "$FLOOR" "$STALLED"
# A startup burst: the whole 7347 arriving inside one tick is what an
# undersized cap looks like, and must report.
assert_reports "startup burst well above the floor" \
    "measurable reclaim stall" 7347 0 "$FLOOR" "$STALLED"
assert_reports "moderate burst from a settled baseline" \
    "measurable reclaim stall" 7847 7347 "$FLOOR" "$STALLED"

echo "== THE HARM GATE: a burst with no measurable stall is not a finding =="
# nousergon-console's real numbers, 2026-08-11: the counter moved every tick
# (693 and climbing) while `full avg300=0.05` and the box had 1535 MB free.
# The kernel reclaiming pages against a soft cap is the cap working, not the
# service suffering. The undersized cap itself remains detected — as a
# CENSORED reading on the console headroom surface, every tick.
assert_silent "burst with background-level stall -> silent" 693 660 "$FLOOR" "$QUIET"
assert_silent "large burst, zero stall -> silent" 7347 0 "$FLOOR" "0.00"

echo "== the gate must not swallow the boundary =="
# At the threshold it reports: the gate suppresses BELOW CGROUP_STALL_MIN only.
assert_reports "stall exactly at the threshold -> reports" \
    "measurable reclaim stall" 700 660 "$FLOOR" "$CGROUP_STALL_MIN"
assert_silent "stall a hair under the threshold -> silent" 700 660 "$FLOOR" "0.99"

echo "== the gate FAILS OPEN: an unreadable stall reports, unjudged =="
# Absence of evidence is not evidence of absence. Every previous defect in this
# check had a silent path that was also its broken path (I4512's unparseable
# PSI predicate; the unwritable state dir; the ec2-user mkdir). A gate that
# went quiet when it could not evaluate itself would join that list.
assert_reports "empty stall reading -> reports, distinctly" \
    "stall reading unavailable" 7347 0 "$FLOOR" ""
assert_reports "omitted stall argument -> reports, distinctly" \
    "stall reading unavailable" 7347 0 "$FLOOR"

echo "== the problem line carries NO varying number =="
# The dedup key is derived from the problem SET, so a count inside the text
# mints a new key every tick and the 60-minute window suppresses nothing. One
# undersized cap published as "33x", then "51x", then "56x" is three alerts
# about one condition. The numbers belong in the journal (stderr).
_line=$(classify_throttle_delta "svc.service" 7347 0 "$FLOOR" "$STALLED" 2>/dev/null)
_line2=$(classify_throttle_delta "svc.service" 9000 0 "$FLOOR" "$STALLED" 2>/dev/null)
if [ "$_line" = "$_line2" ] && [ -n "$_line" ]; then
    echo "ok   - problem text is identical across different deltas (dedup-stable)"
else
    echo "FAIL - problem text varies with the delta; the dedup key will change"
    echo "       every tick and one standing condition re-alerts forever"
    echo "       ('$_line' vs '$_line2')"
    FAILURES=$((FAILURES+1))
fi
if [[ "$_line" =~ [0-9] ]]; then
    echo "FAIL - problem text still contains a digit: '$_line'"
    FAILURES=$((FAILURES+1))
else
    echo "ok   - problem text contains no digits at all"
fi
# ...and the numbers must not be LOST. They go to the journal on stderr.
_journal=$(classify_throttle_delta "svc.service" 7347 0 "$FLOOR" "$STALLED" 2>&1 >/dev/null)
if [[ "$_journal" == *"7347x"* && "$_journal" == *"$STALLED"* ]]; then
    echo "ok   - delta and stall reading are journalled"
else
    echo "FAIL - delta/stall missing from the journal line: '$_journal'"
    FAILURES=$((FAILURES+1))
fi
# The suppressed case must journal too, or the gate's own decision is
# unreconstructible from the box — the transparency test.
_journal=$(classify_throttle_delta "svc.service" 693 0 "$FLOOR" "$QUIET" 2>&1 >/dev/null)
if [[ "$_journal" == *"not reported"* && "$_journal" == *"693x"* ]]; then
    echo "ok   - a SUPPRESSED burst is journalled with its numbers and the reason"
else
    echo "FAIL - suppression is invisible; the gate's decision cannot be audited"
    echo "       from the box alone: '$_journal'"
    FAILURES=$((FAILURES+1))
fi

echo "== the fix must be able to CLEAR the alert =="
# The whole point: after raising the cap and restarting, the counter resets to
# a small number and stops moving. That must read as healthy.
assert_silent "post-remedy: counter reset and steady -> silent" 3 3 "$FLOOR" "$STALLED"

echo "== first run has no baseline =="
# Reporting the lifetime total here would reintroduce the original defect.
assert_silent "no baseline yet -> silent, not the lifetime total" 7347 "" "$FLOOR" "$STALLED"
assert_silent "garbage baseline -> silent" 7347 "n/a" "$FLOOR" "$STALLED"

echo "== a service restart resets the cgroup counter =="
# Counter going backwards is a recreated cgroup, not negative throttling.
assert_silent "counter went backwards (service restarted) -> silent" 5 7347 "$FLOOR" "$STALLED"

echo "== an unreadable counter is a watchdog malfunction, not silence =="
assert_reports "empty current counter is reported" "cannot read cgroup throttle counter" "" 100 "$FLOOR" "$STALLED"
assert_reports "non-numeric current counter is reported" "cannot read cgroup throttle counter" "abc" 100 "$FLOOR" "$STALLED"

echo "== the stall threshold in this test matches the script =="
_src_stall=$(grep -E '^CGROUP_STALL_MIN=' "$TARGET_SCRIPT" | cut -d= -f2)
if [ "$_src_stall" = "$CGROUP_STALL_MIN" ]; then
    echo "ok   - CGROUP_STALL_MIN=$CGROUP_STALL_MIN matches box_health.sh"
else
    echo "FAIL - test threshold $CGROUP_STALL_MIN != box_health.sh CGROUP_STALL_MIN=$_src_stall"
    FAILURES=$((FAILURES+1))
fi

echo "== the call site must pass a stall reading =="
# The argument defaults to empty, and empty FAILS OPEN — so a call site that
# forgot it would not break loudly, it would quietly restore the old noisy
# behaviour and every assertion above would still pass.
# The call is line-continued, so the argument list is matched on its own line
# rather than on the same line as the function name.
if [ -z "$SNAPSHOT_BODY" ]; then
    echo "skip - call-site assertions: snapshot_problems() could not be extracted"
elif printf '%s\n' "$SNAPSHOT_BODY" | grep -q '"\$CGROUP_HIGH_DELTA_MIN" "\$stall"'; then
    echo "ok   - call site passes \$stall as the 5th argument"
else
    echo "FAIL - the call site must pass \$stall; without it the gate fails open"
    echo "       on every unit and the pre-gate noise returns silently"
    FAILURES=$((FAILURES+1))
fi
# ...and $stall must come from avg60 (field 3), not the avg10 the critical
# pressure check uses.
if [ -z "$SNAPSHOT_BODY" ]; then
    echo "skip - avg60 assertion: snapshot_problems() could not be extracted"
elif printf '%s\n' "$SNAPSHOT_BODY" | grep -q 'stall=\$(awk .*split(\$3,kv'; then
    echo "ok   - \$stall is parsed from PSI some avg60 (field 3)"
else
    echo "FAIL - \$stall must be parsed from field 3 (avg60) of the PSI 'some' line"
    FAILURES=$((FAILURES+1))
fi

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
# A harness fault is NOT a finding, and must not share an exit code with one —
# the distinction `check-router-provenance.sh` already draws, and the one whose
# absence made `lib-lockstep-drift-sweep.yml` file 10 records for a drift it
# never measured (alpha-engine-config-I5977). Reported before FAILURES because a
# run that could not measure has nothing to say about the assertions it skipped.
if [ "$HARNESS_FAULTS" -ne 0 ]; then
    echo "HARNESS FAULT - $HARNESS_FAULTS extraction(s) failed; $FAILURES assertion(s) also failed"
    echo "  This is not a box_health.sh finding. The test could not read what it asserts on."
    exit 2
fi
if [ "$FAILURES" -eq 0 ]; then echo "PASS - all classify_throttle_delta assertions"; exit 0; fi
echo "FAILED - $FAILURES assertion(s)"; exit 1
