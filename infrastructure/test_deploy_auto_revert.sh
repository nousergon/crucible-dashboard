#!/bin/bash
# test_deploy_auto_revert.sh — regression test for revert_to_last_good() in
# deploy-on-merge.sh (T1-3, alpha-engine-config-I5250 gap 3).
#
# Policy T1-3 sets the floor: "pinned-SHA deploy, health check after restart,
# automatic revert to the previous SHA on failure." Health checks existed; the
# revert did not, so a bad merge left the box broken until a human noticed. On
# 2026-07-28 a merge did exactly that (a deploy step referencing a repo file on
# a runner with no checkout, exit 127) and recovery was manual.
#
# The properties under test are the ones where a wrong answer is worse than no
# revert at all:
#
#   - No stamp / corrupt stamp -> DO NOT revert. Reverting to a guess could
#     move the box to a sha never validated here. Alert and stop instead.
#   - Stamp == current sha -> nothing to revert to; do not thrash.
#   - The stamp advances ONLY past every health check, so it always names a sha
#     OBSERVED healthy on this box rather than one merely merged.
#   - The revert does not re-invoke deploy-on-merge (that could fail the same
#     way and recurse).
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest:
#   bash infrastructure/test_deploy_auto_revert.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy-on-merge.sh"

# revert_to_last_good() now flocks its git write (config incident
# 2026-08-27 20:07 UTC) — source the real lock-path helper so
# git_sync_lock_path()/$GIT_SYNC_LOCK_WAIT resolve exactly as they do on
# the box, while `flock` itself is stubbed below like every other
# dependency this harness records rather than performs.
. "$SCRIPT_DIR/lib/git-sync-lock.sh"

FAILURES=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILURES=$((FAILURES + 1)); }

# ── Harness: extract revert_to_last_good with its dependencies stubbed ───────
LOG="$TMP/log"
CONSOLE_URL="stub"
REPO_DIR="$TMP/repo"
ALERT_PY="$TMP/alert-py"
LAST_GOOD_SHA_FILE="$TMP/last-good"
CURRENT_SHA="cafebabe0000000000000000000000000000cafe"

log() { echo "$*" >> "$LOG"; }
wait_for_health() { [ "${STUB_HEALTH_OK:-1}" -eq 1 ]; }
# Record invocations instead of performing them.
git() { echo "git $*" >> "$TMP/calls"; return "${STUB_GIT_RC:-0}"; }
systemctl() { echo "systemctl $*" >> "$TMP/calls"; return 0; }
sudo() { shift 2; "$@"; }              # `sudo -u ec2-user git ...` -> git ...
bash() { echo "bash $*" >> "$TMP/calls"; return 0; }
flock() { shift 3; "$@"; }             # `flock -w N lockpath cmd...` -> cmd...
eval "$(awk '/^revert_to_last_good\(\) \{/,/^\}/' "$DEPLOY_SCRIPT")"
declare -F revert_to_last_good >/dev/null || { echo "FAIL - revert_to_last_good() not found"; exit 1; }

reset_harness() { : > "$LOG"; : > "$TMP/calls"; rm -f "$LAST_GOOD_SHA_FILE"; }

echo "== a missing or corrupt stamp must NOT revert =="
# Reverting to a guess is worse than not reverting: it could move the box to a
# sha that was never validated here.
reset_harness
revert_to_last_good "console health check failed" >/dev/null 2>&1
if grep -q "reset --hard" "$TMP/calls" 2>/dev/null; then
    fail "no stamp -> must not run git reset --hard"
else
    pass "no stamp -> does not revert"
fi
grep -q "NOT reverting" "$LOG" && pass "no stamp -> says so in the log" \
    || fail "no stamp -> must log that it is not reverting"

reset_harness
echo "not-a-sha!!" > "$LAST_GOOD_SHA_FILE"
revert_to_last_good "console health check failed" >/dev/null 2>&1
if grep -q "reset --hard" "$TMP/calls" 2>/dev/null; then
    fail "corrupt stamp -> must not run git reset --hard"
else
    pass "corrupt stamp -> does not revert"
fi

echo "== a stamp equal to the current sha must not thrash =="
reset_harness
printf '%s\n' "$CURRENT_SHA" > "$LAST_GOOD_SHA_FILE"
revert_to_last_good "console health check failed" >/dev/null 2>&1
if grep -q "reset --hard" "$TMP/calls" 2>/dev/null; then
    fail "stamp == current -> must not revert to itself"
else
    pass "stamp == current -> does not revert to itself"
fi

echo "== a valid, different stamp reverts and re-provisions =="
reset_harness
printf '%s\n' "deadbeef0000000000000000000000000000dead" > "$LAST_GOOD_SHA_FILE"
revert_to_last_good "console health check failed" >/dev/null 2>&1
grep -q "reset --hard deadbeef" "$TMP/calls" && pass "reverts to the recorded sha" \
    || fail "must git reset --hard to the recorded sha"
# Old code under new units is a state neither sha was tested in.
grep -q "install-box-health.sh" "$TMP/calls" && pass "re-provisions from the reverted tree" \
    || fail "must re-run the installer after reverting"
grep -q "systemctl restart" "$TMP/calls" && pass "restarts services after reverting" \
    || fail "must restart services after reverting"

echo "== the revert must not re-invoke the deploy script (recursion guard) =="
if grep -q "deploy-on-merge.sh" "$TMP/calls" 2>/dev/null; then
    fail "revert must not re-invoke deploy-on-merge.sh — it could fail the same way and recurse"
else
    pass "does not re-invoke deploy-on-merge.sh"
fi

echo "== a failed git reset is reported, not swallowed =="
reset_harness
printf '%s\n' "deadbeef0000000000000000000000000000dead" > "$LAST_GOOD_SHA_FILE"
STUB_GIT_RC=1 revert_to_last_good "console health check failed" >/dev/null 2>&1
grep -q "REVERT FAILED" "$LOG" && pass "git reset failure is logged loudly" \
    || fail "a failed git reset must be reported"

echo "== source-level invariants =="
# The stamp must advance only past every health check, or it records a sha that
# was merged rather than one observed healthy.
body=$(awk '/^# Advance the stamp ONLY here/,/LAST_GOOD_SHA_FILE"/' "$DEPLOY_SCRIPT")
[ -n "$body" ] && pass "stamp advance is guarded by a comment stating the invariant" \
    || fail "stamp advance block not found"
stamp_ln=$(grep -n '> "\$LAST_GOOD_SHA_FILE"' "$DEPLOY_SCRIPT" | head -1 | cut -d: -f1)
health_ln=$(grep -n 'if \[ -n "\$health_failed" \]; then' "$DEPLOY_SCRIPT" | head -1 | cut -d: -f1)
if [ -n "$stamp_ln" ] && [ -n "$health_ln" ] && [ "$stamp_ln" -gt "$health_ln" ]; then
    pass "stamp is written after the health-check gate, not before"
else
    fail "stamp must be written AFTER the health-check gate (stamp=$stamp_ln gate=$health_ln)"
fi
grep -q 'revert_to_last_good "\$health_failed' "$DEPLOY_SCRIPT" \
    && pass "health failure routes to the revert" \
    || fail "a failed health check must call revert_to_last_good"

echo
if [ "$FAILURES" -eq 0 ]; then echo "PASS - all auto-revert assertions"; exit 0; fi
echo "FAILED - $FAILURES assertion(s)"; exit 1
