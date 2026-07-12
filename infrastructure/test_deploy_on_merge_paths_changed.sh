#!/bin/bash
# test_deploy_on_merge_paths_changed.sh — regression test for the
# paths_changed() diff-gate helper in deploy-on-merge.sh (config#2242).
#
# Root cause under test: `git diff ... | grep -q PATTERN` is unsafe under
# `set -o pipefail`. GNU grep's `-q` exits on the first match and closes
# its end of the pipe; `git diff` writes its output to a pipe in ~4KB
# stdio-buffered chunks (not one write() per line), so a diff spanning
# more than one chunk can have git diff still writing a later chunk after
# grep has already exited. That write gets SIGPIPE, git diff exits 141,
# and pipefail propagates the 141 as the pipeline's exit status even
# though grep DID match. `if git diff ... | grep -q ...; then` then
# evaluates false on a genuinely-true diff — silently skipping installer
# re-runs (this is exactly what happened to PR385's box-health installer
# gate). paths_changed() fixes this by capturing git diff's output and
# exit code with no pipe in between, and fails loud + fails safe (assumes
# "changed") on a real git error instead of silently swallowing it.
#
# No pytest/Make harness exists for this script's shell logic (the repo's
# tests/ dir is pytest for the Streamlit app), so this is a minimal
# self-contained bash test runner, invoked directly:
#   bash infrastructure/test_deploy_on_merge_paths_changed.sh
# Exits 0 if all assertions pass, non-zero (with a line per failure) if not.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/deploy-on-merge.sh"

FAILURES=0
assert_true() {
    local desc="$1"; shift
    if "$@"; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc (expected true, got false/error)"
        FAILURES=$((FAILURES + 1))
    fi
}
assert_false() {
    local desc="$1"; shift
    if "$@"; then
        echo "FAIL - $desc (expected false, got true)"
        FAILURES=$((FAILURES + 1))
    else
        echo "ok   - $desc"
    fi
}

# Pull just log(), fail(), and paths_changed() out of the real script
# rather than sourcing the whole file (sourcing would also execute the
# script's top-level deploy logic). Each is a single-line-start,
# single-line-end (`}` alone on its own line) function definition, so we
# extract by exact start-marker -> next standalone-`}` line range.
extract_fn() {
    local start_pattern="$1"
    local start end
    start=$(grep -n "$start_pattern" "$TARGET_SCRIPT" | head -1 | cut -d: -f1)
    if [ -z "$start" ]; then
        echo "FAIL - could not find '$start_pattern' in $TARGET_SCRIPT (script structure changed?)" >&2
        exit 1
    fi
    end=$(awk -v s="$start" 'NR>=s && /^}$/{print NR; exit}' "$TARGET_SCRIPT")
    sed -n "${start},${end}p" "$TARGET_SCRIPT"
}
eval "$(extract_fn '^log() {')"
eval "$(extract_fn '^fail() {')"
eval "$(extract_fn '^paths_changed() {')"

if ! declare -F paths_changed >/dev/null; then
    echo "FAIL - paths_changed() not found after extraction from $TARGET_SCRIPT"
    exit 1
fi

LOG=$(mktemp)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"; rm -f "$LOG"' EXIT

# The real gates run `sudo -u ec2-user git diff ...`. In this test harness
# we're operating on a throwaway repo as the current user, so shadow `sudo`
# with a passthrough shim that drops the `-u ec2-user` bit and just runs
# the rest of the command as-is (avoids requiring passwordless sudo / an
# ec2-user account in CI).
sudo() {
    if [ "$1" = "-u" ]; then
        shift 2
    fi
    "$@"
}

# ── Fixture: a scratch repo with a large multi-file diff, shaped like the
# real §2b-2e multi-path box-health diff that triggered config#2242 (big
# enough to span multiple ~4KB git-diff stdio writes). ────────────────────
git -C "$WORK" init -q
git -C "$WORK" config user.email test@test.local
git -C "$WORK" config user.name test

mkdir -p "$WORK/infrastructure"
for f in a.sh b.sh c.sh; do
    : > "$WORK/infrastructure/$f"
done
git -C "$WORK" add -A && git -C "$WORK" commit -q -m base

# Make each file large enough that the combined diff spans several 4KB
# stdio writes (matched the real repro: ~9.7KB across 3 writes).
for f in a.sh b.sh c.sh; do
    for i in $(seq 1 300); do
        echo "echo line $i of $f" >> "$WORK/infrastructure/$f"
    done
done
git -C "$WORK" add -A && git -C "$WORK" commit -q -m "big multi-file change"

OLD_SHA=$(git -C "$WORK" rev-parse HEAD~1)
NEW_SHA=$(git -C "$WORK" rev-parse HEAD)

cd "$WORK"

echo "=== paths_changed regression tests (config#2242) ==="

# 1. Real, large, multi-file diff must be detected as changed, reliably —
#    not flaky under pipefail. Run it many times: before the fix this
#    failed ~99/100 times on a diff this shape.
pass=0
for i in $(seq 1 25); do
    if paths_changed "$OLD_SHA" "$NEW_SHA" infrastructure/a.sh infrastructure/b.sh infrastructure/c.sh; then
        pass=$((pass + 1))
    fi
done
if [ "$pass" -eq 25 ]; then
    echo "ok   - large multi-file diff detected as changed, 25/25 runs (no pipefail/SIGPIPE flake)"
else
    echo "FAIL - large multi-file diff only detected changed $pass/25 runs (pipefail/SIGPIPE regression)"
    FAILURES=$((FAILURES + 1))
fi

# 2. Genuine no-change (same SHA on both sides) must report false.
assert_false "no-op diff (same SHA both sides) reports unchanged" \
    paths_changed "$NEW_SHA" "$NEW_SHA" infrastructure/a.sh

# 3. A git-diff error (bad revision) must NOT be silently swallowed into
#    "unchanged" — it must fail loud (logged) and fail safe (report
#    changed, so the caller re-runs its idempotent installer).
: > "$LOG"
assert_true "git-diff error (bad revision) fails safe as 'changed', not silently skipped" \
    paths_changed "deadbeef0000~1" "deadbeef0000" infrastructure/a.sh
if grep -q "WARN git diff" "$LOG"; then
    echo "ok   - git-diff error was logged loudly"
else
    echo "FAIL - git-diff error was NOT logged (should log WARN on failure)"
    FAILURES=$((FAILURES + 1))
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL PASS"
    exit 0
else
    echo "$FAILURES assertion(s) FAILED"
    exit 1
fi
