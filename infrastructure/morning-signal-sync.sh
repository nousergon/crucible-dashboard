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
#   4. Records the OUTCOME to a small state file (git_sync_state_path) —
#      SYNC_OK, the HEAD_SHA the checkout ended on, and SYNCED_AT — on
#      EVERY exit path, success or failure. This is what lets
#      box_health.sh's check_morning_signal_sync_drift detect a stale run
#      (alpha-engine-config-I8990): morning-signal.service and
#      -bakeoff.service invoke this via a failure-tolerant `ExecStartPre=-`
#      (below), so a failed sync does not stop the episode from generating
#      on whatever code was already on disk, and the unit still exits 0.
#      Without this record, nothing anywhere said a run used stale code.
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
GIT_SYNC_STATE="$(git_sync_state_path "$CHECKOUT_DIR")"
# Overridable only for tests (staging an unreachable remote to demonstrate
# the failure path without a real 40s wait) — production takes the defaults.
FETCH_RETRIES="${AE_GIT_FETCH_RETRIES:-5}"
FETCH_SLEEP="${AE_GIT_FETCH_SLEEP:-10}"

# `-w` bounds the wait; a flock timeout is a genuinely stuck git writer on
# this checkout, not a swallowable condition — same fail-loud semantics as
# boot-pull.sh's lock. The body runs in a child bash WITHOUT `set -e` (unlike
# before I8990) so every exit path — success, an exhausted fetch retry, a
# missing origin/main, or a failed reset — reaches write_state before
# exiting; each branch below checks its own command and exits explicitly
# rather than relying on the shell to bail. All inputs are passed as
# positional args, not inherited env, so nothing depends on export.
flock -w "$GIT_SYNC_LOCK_WAIT" "$GIT_SYNC_LOCK" bash -c '
    set -uo pipefail
    checkout_dir="$1"
    fetch_retries="$2"
    fetch_sleep="$3"
    state_path="$4"

    # write_state OK HEAD_SHA — atomic (write to a tmp file in the same
    # directory, then mv). $state_path lives under /tmp, alongside the lock
    # it is named after (git_sync_state_path), never inside the checkout
    # itself — reset --hard only touches tracked/ignored paths, but a state
    # file the detector must survive a reset belongs outside the tree it
    # describes on principle, same as the lock.
    write_state() {
        local ok="$1" sha="$2" tmp
        tmp="$(mktemp "${state_path}.XXXXXX" 2>/dev/null)" || return 0
        {
            printf "SYNC_OK=%s\n" "$ok"
            printf "HEAD_SHA=%s\n" "$sha"
            printf "SYNCED_AT=%s\n" "$(date +%s)"
        } > "$tmp" 2>/dev/null && mv -f "$tmp" "$state_path" 2>/dev/null
    }

    current_head() {
        git rev-parse HEAD 2>/dev/null || echo unknown
    }

    if ! cd "$checkout_dir"; then
        echo "ERROR: cannot cd into ${checkout_dir}" >&2
        write_state 0 unknown
        exit 1
    fi

    attempt=1
    while :; do
        if git fetch origin --quiet; then
            break
        fi
        if [ "$attempt" -ge "$fetch_retries" ]; then
            echo "ERROR: git fetch origin failed after ${fetch_retries} attempts in ${checkout_dir}" >&2
            write_state 0 "$(current_head)"
            exit 1
        fi
        echo "WARN: git fetch origin failed (attempt ${attempt}/${fetch_retries}) in ${checkout_dir} — retrying in ${fetch_sleep}s" >&2
        sleep "$fetch_sleep"
        attempt=$((attempt + 1))
    done

    if ! git rev-parse --verify --quiet origin/main > /dev/null; then
        echo "ERROR: origin/main not present in ${checkout_dir} after a successful fetch — refusing to reset --hard onto a missing ref" >&2
        write_state 0 "$(current_head)"
        exit 1
    fi

    if ! git reset --hard origin/main; then
        echo "ERROR: git reset --hard origin/main failed in ${checkout_dir}" >&2
        write_state 0 "$(current_head)"
        exit 1
    fi

    write_state 1 "$(current_head)"
' _ "$CHECKOUT_DIR" "$FETCH_RETRIES" "$FETCH_SLEEP" "$GIT_SYNC_STATE"
