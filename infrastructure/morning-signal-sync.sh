#!/bin/bash
# morning-signal-sync.sh — the single git-sync path every morning-signal
# on-box caller uses to refresh /home/ec2-user/morning-signal to
# origin/main. Replaces the inline `git fetch` + `git reset --hard` pairs
# previously duplicated across morning-signal-pull.service,
# morning-signal.service, morning-signal-bakeoff.service and
# morning-signal-recover.sh — none of which held any lock
# (alpha-engine-config incident class, see infrastructure/lib/
# git-sync-lock.sh for the full incident history).
#
# What this adds beyond a bare fetch+reset:
#   1. Takes the checkout's flock (git-sync-lock.sh) so this checkout's
#      writers can no longer race each other's ref updates.
#   2. Retries `git fetch` up to 5 times (10s sleep) — a transient network
#      blip must not be indistinguishable from a genuinely broken remote.
#   3. Asserts origin/main is actually present after the fetch
#      (`git rev-parse --verify --quiet`) BEFORE resetting to it — so an
#      exhausted retry fails loud with a clear message instead of
#      `reset --hard` silently resetting to a stale/absent ref.
#
# Exit code is the caller's signal: this script itself always fails loud
# on an exhausted retry or a flock timeout (`set -e`). Whether that
# failure may be tolerated is the CALLER's decision, not this script's —
# morning-signal.service and morning-signal-bakeoff.service deliberately
# invoke this via a failure-tolerant `ExecStartPre=-` (best-effort:
# episode generation must never be skipped by a transient sync blip; see
# those units' headers), while morning-signal-pull.service's ExecStart has
# no `-` and fails the unit loud, and morning-signal-recover.sh wraps the
# call in `||` to keep its existing WARN-and-continue behavior.
#
# Usage: morning-signal-sync.sh [checkout-dir]
#   checkout-dir defaults to /home/ec2-user/morning-signal.

set -euo pipefail

CHECKOUT_DIR="${1:-/home/ec2-user/morning-signal}"

# Two-candidate resolution, same pattern as boot-pull.sh's ALERT_PY lookup:
# this script runs BOTH in place (infrastructure/lib/git-sync-lock.sh is a
# subdir sibling) and installed to /usr/local/bin/morning-signal-sync.sh
# (install-morning-signal.sh installs git-sync-lock.sh alongside it there,
# flat — no lib/ subdir at that destination).
for _gsl in "$(dirname "${BASH_SOURCE[0]}")/lib/git-sync-lock.sh" \
            "$(dirname "${BASH_SOURCE[0]}")/git-sync-lock.sh"; do
    if [ -r "$_gsl" ]; then . "$_gsl"; break; fi
done
unset _gsl

GIT_SYNC_LOCK="$(git_sync_lock_path "$CHECKOUT_DIR")"
FETCH_RETRIES=5
FETCH_SLEEP=10

# `-w` bounds the wait; a flock timeout is a genuinely stuck git writer on
# this checkout, not a swallowable condition — same fail-loud semantics as
# boot-pull.sh's lock. The body runs in a child bash so `set -e` there
# can't be short-circuited by the enclosing `flock` invocation's own
# exit-status plumbing; all inputs are passed as positional args, not
# inherited env, so nothing depends on export.
flock -w "$GIT_SYNC_LOCK_WAIT" "$GIT_SYNC_LOCK" bash -c '
    set -euo pipefail
    checkout_dir="$1"
    fetch_retries="$2"
    fetch_sleep="$3"

    cd "$checkout_dir"

    attempt=1
    while :; do
        if git fetch origin --quiet; then
            break
        fi
        if [ "$attempt" -ge "$fetch_retries" ]; then
            echo "ERROR: git fetch origin failed after ${fetch_retries} attempts in ${checkout_dir}" >&2
            exit 1
        fi
        echo "WARN: git fetch origin failed (attempt ${attempt}/${fetch_retries}) in ${checkout_dir} — retrying in ${fetch_sleep}s" >&2
        sleep "$fetch_sleep"
        attempt=$((attempt + 1))
    done

    if ! git rev-parse --verify --quiet origin/main > /dev/null; then
        echo "ERROR: origin/main not present in ${checkout_dir} after a successful fetch — refusing to reset --hard onto a missing ref" >&2
        exit 1
    fi

    git reset --hard origin/main
' _ "$CHECKOUT_DIR" "$FETCH_RETRIES" "$FETCH_SLEEP"
