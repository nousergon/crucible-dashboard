#!/bin/bash
# test_installer_routing.sh — regression test for the §2f installer-routing
# helpers in deploy-on-merge.sh (alpha-engine-config-I5215).
#
# The bug it guards: six install-*.sh scripts provision state OUTSIDE the git
# tree (systemd units, /usr/local/bin scripts, agent configs, CloudWatch
# alarms) and were invoked by nothing automated. A `git pull` never touches
# those paths, so the repo and the running box drifted indefinitely while every
# check read green. Found when config-I5209's timer thresholds merged, deployed
# green, and did not exist on the box (crucible-dashboard-PR570).
#
# What is asserted here is the STAMP predicate, which is the part with real
# logic. The `files` mode reuses any_file_state_stale(), already covered.
#
# The stamp's contract, and why each case matters:
#   - unchanged inputs      -> fresh   (a gate that always fires is not a gate)
#   - any input changed     -> stale
#   - no stamp yet          -> stale   (first deploy must install)
#   - unreadable input      -> stale   (fail SAFE: run it and fail loudly there,
#                                       never silently skip forever)
#   - failed install        -> stale next time (stamp is written only on success)
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest:
#   bash infrastructure/test_installer_routing.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy-on-merge.sh"

FAILURES=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Extract the three stamp helpers without executing the rest of the script.
for fn in installer_inputs_digest installer_stamp_stale installer_stamp_write; do
    eval "$(awk "/^${fn}\(\)/,/^\}/" "$DEPLOY_SCRIPT")"
    declare -F "$fn" >/dev/null || { echo "FAIL - $fn() not found in deploy-on-merge.sh"; exit 1; }
done

STAMP_DIR="$TMP/stamps"

check() {
    local desc="$1" want="$2"; shift 2
    local got
    if installer_stamp_stale "$@"; then got=stale; else got=fresh; fi
    if [ "$got" = "$want" ]; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc (expected $want, got $got)"
        FAILURES=$((FAILURES + 1))
    fi
}

echo "a" > "$TMP/in1"
echo "b" > "$TMP/in2"

echo "== first deploy: no stamp yet =="
check "no stamp recorded -> stale" stale demo.sh "$TMP/in1" "$TMP/in2"

echo "== after a successful install =="
installer_stamp_write demo.sh "$TMP/in1" "$TMP/in2"
check "unchanged inputs -> fresh" fresh demo.sh "$TMP/in1" "$TMP/in2"

echo "== any input changing must re-run =="
echo "a-modified" > "$TMP/in1"
check "first input changed -> stale" stale demo.sh "$TMP/in1" "$TMP/in2"
installer_stamp_write demo.sh "$TMP/in1" "$TMP/in2"
echo "b-modified" > "$TMP/in2"
check "second input changed -> stale" stale demo.sh "$TMP/in1" "$TMP/in2"
installer_stamp_write demo.sh "$TMP/in1" "$TMP/in2"
check "re-stamped after change -> fresh" fresh demo.sh "$TMP/in1" "$TMP/in2"

echo "== order and concatenation are not confusable =="
# A digest over concatenated content must not collapse two different splits of
# the same bytes, or a swap between two inputs would read as no change.
printf 'xy\n' > "$TMP/s1"; printf 'z\n'  > "$TMP/s2"
installer_stamp_write split.sh "$TMP/s1" "$TMP/s2"
printf 'x\n'  > "$TMP/s1"; printf 'yz\n' > "$TMP/s2"
check "different content with same total bytes -> stale" stale split.sh "$TMP/s1" "$TMP/s2"

echo "== fail SAFE, never silently fresh =="
check "missing input -> stale" stale demo.sh "$TMP/in1" "$TMP/does-not-exist"
STAMP_DIR="$TMP/unwritable-dir/nested"
check "unreadable stamp dir -> stale" stale demo.sh "$TMP/in1" "$TMP/in2"
STAMP_DIR="$TMP/stamps"

echo "== a failed install must not be recorded as done =="
# installer_stamp_write is called ONLY after a successful run in deploy-on-merge.sh.
# Guard that the ordering is still in the source, since the test cannot run the
# real installers.
# Compared by line NUMBER rather than a fixed -A window, so the assertion does
# not silently pass or fail when the intervening comment changes length.
_run_ln=$(grep -n 'bash "$INFRA/$_name"' "$DEPLOY_SCRIPT" | head -1 | cut -d: -f1)
_stamp_ln=$(grep -n 'installer_stamp_write "$_name"' "$DEPLOY_SCRIPT" | head -1 | cut -d: -f1)
if [ -n "$_run_ln" ] && [ -n "$_stamp_ln" ] && [ "$_stamp_ln" -gt "$_run_ln" ]; then
    echo "ok   - stamp is written after the install, not before"
else
    echo "FAIL - stamp must be written only after a successful install"
    FAILURES=$((FAILURES + 1))
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "PASS - all installer routing assertions"
    exit 0
fi
echo "FAILED - $FAILURES assertion(s)"
exit 1
