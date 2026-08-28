#!/bin/bash
# test_box_health_alert_lifecycle.sh — regression test for the open/clear pair
# in box_health.sh (alpha-engine-config-I8105).
#
# Root cause under test: every alert this box emitted was WRITE-ONCE. A CRITICAL
# went out on detection and NOTHING went out when the condition ended, so no
# page could be told from a live outage. Measured 2026-08-21 on
# i-09b539c844515d549: litellm-config-reconcile.timer failed 18:40:28 and
# 18:50:28 UTC and recovered 18:53:12; ops-config-drift.timer failed 20:02:30
# and 20:09:49 and recovered 20:23:47. Three CRITICAL pages, zero all-clears.
# Both pages were CORRECT when sent — nothing here touches detection.
#
# WHAT THIS ASSERTS, and why each one is the half that gets written wrong:
#
#   1. A key present last run and absent now emits EXACTLY ONE clear, carrying
#      that key. This is the deliverable.
#   2. A key present in BOTH runs emits NO clear. The failure mode on the other
#      side of the diff is a terminator for a condition that is still live,
#      which is worse than the silence it replaces — it tells a human the
#      outage is over while it is running.
#   3. A key that is NEW emits no clear. Reading the difference in the wrong
#      direction produces a clear for the condition that just started.
#   4. An EMPTY prior emits nothing. Empty means "nothing was alerted" (first
#      run after a deploy, a reboot, or a recreated state dir) and must never
#      be read as "everything cleared" — that would page an all-clear storm on
#      every deploy.
#   5. The clear names the problem LINES the page carried, not just its key.
#   6. A krepis without publish_clear counts the clear as unpublished instead
#      of attempting a call that argparse rejects with exit 2 — a version skew
#      must be visible as a version skew, not as a delivery failure.
#   7. alerted_state_lifecycle returns still_open for a key the prior carried
#      and opened otherwise (the `state` field on the emitted record).
#   8. A CLEAR INHERITS THE DESTINATION OF THE PAGE IT TERMINATES
#      (alpha-engine-config-I9044). Field 2 of each state row is the opening
#      page's severity, which this file already wrote and already carried
#      across runs; publish_clears used to discard it and send every
#      terminator into the operator's chat. A page that buzzed the phone still
#      clears in the channel — the `timer job failing:` findings that dominate
#      this box are `critical`, and their RESOLVED messages are unchanged. A
#      page that never buzzed is recorded on the console instead, and is NOT
#      counted as unpublished: it was delivered, to the other surface.
#
# The functions under test are pure functions of their arguments plus two file
# paths, so this runs without systemd, AWS, or a real krepis — the whole point
# of extracting them rather than inlining the diff at the call site.
#
# Invoked by tests/test_dash_deploy_infra.py so CI actually runs it; also
# runnable directly:
#   bash infrastructure/test_box_health_alert_lifecycle.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_health.sh"

if [ ! -r "$TARGET_SCRIPT" ]; then
    echo "FAIL - cannot read $TARGET_SCRIPT"
    exit 1
fi

# Source only the functions under test. box_health.sh runs checks at load time,
# so extract the definitions rather than sourcing the whole script.
for _fn in alerted_state_prior alerted_state_lifecycle alerted_state_write \
           clear_destination krepis_push_set_load publish_channel_clear \
           krepis_clear_supported publish_clears; do
    fn=$(awk -v f="^${_fn}\\\\(\\\\) \\\\{" '$0 ~ f,/^\}/' "$TARGET_SCRIPT")
    if [ -z "$fn" ]; then
        echo "FAIL - ${_fn}() not found in box_health.sh"
        exit 1
    fi
    eval "$fn"
done

TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

THROTTLE_STATE_DIR="$TMPDIR_T"
ALERTED_STATE="$TMPDIR_T/alerted-problems"
INSTANCE_ID="i-test"
UNPUBLISHED_CLEARS=0
# The console-route accumulators publish_clears fills. Initialised, not merely
# declared: `set -u` turns an unset accumulator into a dead watchdog.
CONSOLE_ROUTED_LINES=""
CONSOLE_CLEAR_KEYS=()
CONSOLE_CLEAR_MSGS=()
# Pre-set so krepis_push_set_load returns without shelling out. The set itself
# is krepis' (SEVERITY_PHONE_PUSH); what is under test is what publish_clears
# does with each ANSWER, not how the answer is obtained.
KREPIS_PUSH_SET="critical error"
KREPIS_CLEAR=""

# Stand-in for the krepis CLI: records every invocation instead of publishing.
CALLS="$TMPDIR_T/calls"
: > "$CALLS"
SUPPORTS_CLEAR=1
ALERT_PY="$TMPDIR_T/fake_alert_py"
cat > "$ALERT_PY" <<'FAKE'
#!/bin/bash
printf '%s\n' "$*" >> "$CALLS_FILE"
exit 0
FAKE
chmod +x "$ALERT_PY"
export CALLS_FILE="$CALLS"

# krepis_supports_clear is overridden rather than extracted: the real one shells
# out to a live interpreter, and the behaviour under test is what publish_clears
# does with each ANSWER, not how the answer is obtained.
krepis_supports_clear() { [ "$SUPPORTS_CLEAR" -eq 1 ]; }

PASS=0
FAIL=0

check() {
    local desc="$1" got="$2" want="$3"
    if [ "$got" = "$want" ]; then
        PASS=$((PASS + 1))
        echo "  ok   - $desc"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL - $desc"
        echo "         want: $want"
        echo "         got:  $got"
    fi
}

reset_calls() {
    : > "$CALLS"
    UNPUBLISHED_CLEARS=0
    CONSOLE_ROUTED_LINES=""
    CONSOLE_CLEAR_KEYS=()
    CONSOLE_CLEAR_MSGS=()
    KREPIS_CLEAR=""
}

# How many terminators were routed to the console this call.
console_clear_count() { echo "${#CONSOLE_CLEAR_KEYS[@]}"; }

TAB=$'\t'
prior_two="keyA${TAB}critical${TAB}timer job failing: alpha.timer
keyA${TAB}critical${TAB}timer job failing: beta.timer
keyB${TAB}warning${TAB}disk high: root >=80% used"

echo "== 1/3. a gone key clears; a surviving key does not; a new key does not =="
reset_calls
publish_clears "$prior_two" "keyB${TAB}warning${TAB}disk high: root >=80% used
keyC${TAB}critical${TAB}service down: metron-api"
check "exactly one clear published" "$(grep -c 'krepis.alerts clear' "$CALLS")" "1"
check "the clear carries the GONE key" \
      "$(grep -c -- '--identity-key keyA' "$CALLS")" "1"
check "no clear for the surviving key" \
      "$(grep -c -- '--identity-key keyB' "$CALLS")" "0"
check "no clear for the new key" \
      "$(grep -c -- '--identity-key keyC' "$CALLS")" "0"
check "no unpublished clears counted" "$UNPUBLISHED_CLEARS" "0"

echo "== 4. an empty prior emits nothing =="
reset_calls
publish_clears "" "keyC${TAB}critical${TAB}service down: metron-api"
check "empty prior publishes no clear" "$(wc -l < "$CALLS" | tr -d ' ')" "0"

echo "   ...and an empty CURRENT clears everything that was standing"
reset_calls
publish_clears "$prior_two" ""
check "the critical-opener key cleared in the channel" \
      "$(grep -c 'krepis.alerts clear' "$CALLS")" "1"
check "the warning-opener key was routed to the console instead" \
      "$(console_clear_count)" "1"
check "neither clear was counted as unpublished" "$UNPUBLISHED_CLEARS" "0"

echo "== 5. the clear names the lines the page carried =="
reset_calls
publish_clears "$prior_two" ""
check "clear body names the first line" \
      "$(grep -c 'timer job failing: alpha.timer' "$CALLS")" "1"
check "clear body names the second line of the SAME page" \
      "$(grep -c 'timer job failing: beta.timer' "$CALLS")" "1"
check "clear body is marked as a resolution" \
      "$(grep -c 'health alert resolved' "$CALLS")" "1"
check "the console record names the key of the page it terminates" \
      "$(printf '%s' "$CONSOLE_ROUTED_LINES" | grep -c '^clear: keyB ')" "1"
check "the console record names the line that page carried" \
      "$(printf '%s' "$CONSOLE_ROUTED_LINES" | grep -c 'disk high: root >=80% used')" "1"

echo "== 6. a krepis without publish_clear counts, never calls =="
reset_calls
SUPPORTS_CLEAR=0
publish_clears "$prior_two" ""
check "no CLI call attempted on an unsupporting krepis" \
      "$(wc -l < "$CALLS" | tr -d ' ')" "0"
check "only the CHANNEL-bound clear counts as unpublished" "$UNPUBLISHED_CLEARS" "1"
check "the console-bound clear is unaffected by the krepis pin" \
      "$(console_clear_count)" "1"
SUPPORTS_CLEAR=1

echo "== 7. alerted_state_lifecycle: still_open vs opened =="
alerted_state_write "$prior_two"
check "a key the prior carried is still_open" \
      "$(alerted_state_lifecycle keyA)" "still_open"
check "a key the prior did not carry is opened" \
      "$(alerted_state_lifecycle keyZ)" "opened"
check "a key that is a PREFIX of a stored key is not a match" \
      "$(alerted_state_lifecycle key)" "opened"
rm -f "$ALERTED_STATE"
check "no state file at all reads as opened, never as still_open" \
      "$(alerted_state_lifecycle keyA)" "opened"

echo "== 8. a clear inherits the destination of the page it terminates =="
check "a critical opener clears in the channel" "$(clear_destination critical)" "channel"
check "an error opener clears in the channel"   "$(clear_destination error)"    "channel"
check "a warning opener clears on the console"  "$(clear_destination warning)"  "console"
check "an info opener clears on the console"    "$(clear_destination info)"     "console"
# The severity field is what the state file carried. A row it could not be read
# from is a page we cannot PROVE was quiet, and an owed terminator is never
# withheld on a guess — same direction as classify_problem_severity's default arm.
check "an unreadable severity falls back to the channel" \
      "$(clear_destination "")" "channel"

echo "   ...and the push set is krepis', not a copy: narrowing it moves the route"
KREPIS_PUSH_SET="critical"
check "error stops clearing in the channel when krepis stops pushing it" \
      "$(clear_destination error)" "console"
KREPIS_PUSH_SET="critical error"

echo
echo "passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ]
