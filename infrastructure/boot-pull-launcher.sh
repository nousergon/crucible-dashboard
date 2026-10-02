#!/bin/bash
# boot-pull-launcher.sh — stable launcher for boot-pull.sh, installed OUTSIDE
# the alpha-engine-dashboard checkout (/usr/local/sbin) so systemd never execs
# a script from the tree that script itself rewrites.
#
# Port of crucible-executor's infrastructure/boot-pull-launcher.sh
# (crucible-executor-PR495 + PR533), alpha-engine-config-I8734's dashboard-box
# half. boot-pull.service used to point ExecStart straight at
# /home/ec2-user/alpha-engine-dashboard/infrastructure/boot-pull.sh — INSIDE
# the checkout boot-pull.sh fast-forwards (sync_repo_to_main) and, on a failed
# health gate, hard-resets back to the previous SHA. Bash reads a running
# script incrementally; whether a rewrite underneath it is picked up depends
# on whether the writer replaces the inode or edits it in place, which is not
# a property anything in this repo chose or tests.
#
# This launcher is the only thing systemd execs. It snapshots boot-pull.sh to
# a private path outside the synced tree and execs THAT, so nothing the run
# does to the checkout can reach the bytes bash is executing.
#
# ── Installed by two writers, ordered so the unit can always start ─────────
# install-boot-pull.sh (run by deploy-on-merge.sh's ROUTED_INSTALLERS row when
# the unit, timer or this file drifts) and boot-pull.sh itself (every hourly
# run) both install this file to /usr/local/sbin BEFORE they copy a
# boot-pull.service whose ExecStart names it, and boot-pull.sh refuses to copy
# that unit while the launcher is not installed and executable. A unit that
# ExecStarts a missing file fails 203/EXEC every hour, and boot-pull is the
# thing that would have repaired it — measured on the trading box
# 2026-08-28..31 (crucible-executor-PR519).
#
# ── The snapshot is taken from origin/main, not from the working tree ─────
# alpha-engine-config-I9832 / I9829 (crucible-executor-PR533): snapshotting the
# tree and only then letting boot-pull.sh sync it runs, on run N, the code the
# tree held at the end of run N-1 — every boot-pull fix lands one run late.
# The launcher reads the file straight out of the fetched ref:
#
#   git fetch origin main && git show origin/main:infrastructure/boot-pull.sh
#
# Reading rather than resetting is load-bearing: boot-pull.sh records
# PREV_SHA inside its own sync loop and its health gate reverts to it. A
# launcher that moved HEAD first would make PREV_SHA == NEW_SHA and turn that
# revert into a no-op. Reading a blob changes no ref the revert depends on
# and no file in the working tree.
#
# Safe this early because: crucible-dashboard is PUBLIC (no credential, so a
# broken credential path cannot block its own repair); the fetch takes the
# same per-checkout flock every other writer on this checkout takes
# (infrastructure/lib/git-sync-lock.sh); and every failure falls through to
# the on-disk copy, never blocking the run.
set -euo pipefail

SRC="/home/ec2-user/alpha-engine-dashboard/infrastructure/boot-pull.sh"
SNAPSHOT="/home/ec2-user/.boot-pull-snapshot.sh"
# Overridable ONLY so the test suite can point this at a sandbox checkout;
# production uses the defaults and the unit file sets none of them.
REPO="${AE_LAUNCHER_REPO:-/home/ec2-user/alpha-engine-dashboard}"
REPO_PATH="${AE_LAUNCHER_REPO_PATH:-infrastructure/boot-pull.sh}"
# Same derivation as git_sync_lock_path() in infrastructure/lib/git-sync-lock.sh
# (computed here, not sourced: sourcing it would execute a file from the tree
# this launcher exists to stop executing from). tests/
# test_dashboard_boot_pull_launcher.py pins that the two agree.
SYNC_LOCK="${AE_GIT_SYNC_LOCK:-/tmp/nousergon-git-sync-$(basename "$REPO").lock}"
RUN_AS="${AE_LAUNCHER_RUN_AS:-ec2-user}"

# git runs as the checkout's owner under the checkout's lock. -w 150 matches
# GIT_SYNC_LOCK_WAIT's default.
run_git() {
    sudo -u "$RUN_AS" -H flock -w 150 "$SYNC_LOCK" git -C "$REPO" "$@"
}

# Writes the origin/main copy of boot-pull.sh to $SNAPSHOT and returns 0, or
# returns non-zero having written nothing. Never exits the script: the caller
# falls back to the on-disk copy on any failure.
snapshot_from_origin() {
    local tmp
    tmp="${SNAPSHOT}.fetching.$$"

    run_git rev-parse --git-dir >/dev/null 2>&1 || {
        echo "boot-pull-launcher: $REPO is not a readable git checkout — falling back to the on-disk copy" >&2
        return 1
    }

    if ! run_git fetch --quiet origin main 2>/dev/null; then
        echo "boot-pull-launcher: fetch of origin/main failed (network not up yet?) — falling back to the on-disk copy; boot-pull's own sync retries" >&2
        return 1
    fi

    if ! run_git show "origin/main:${REPO_PATH}" > "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        echo "boot-pull-launcher: origin/main has no ${REPO_PATH} — falling back to the on-disk copy" >&2
        return 1
    fi

    # A zero-length blob would exec as a no-op and report success — boot-pull
    # would appear to have run and done nothing at all.
    if [ ! -s "$tmp" ]; then
        rm -f "$tmp"
        echo "boot-pull-launcher: origin/main:${REPO_PATH} is empty — falling back to the on-disk copy" >&2
        return 1
    fi

    if [ -f "$SRC" ] && ! cmp -s "$tmp" "$SRC"; then
        echo "boot-pull-launcher: SNAPSHOT WAS STALE — origin/main:${REPO_PATH} differs from the on-disk copy at $SRC; running the origin/main version." >&2
    fi

    mv "$tmp" "$SNAPSHOT"
    return 0
}

if ! snapshot_from_origin; then
    if [ ! -f "$SRC" ]; then
        echo "boot-pull-launcher: $SRC not found and origin/main unreadable — alpha-engine-dashboard checkout missing or not yet cloned" >&2
        exit 1
    fi
    cp "$SRC" "$SNAPSHOT"
fi

chmod 700 "$SNAPSHOT"
exec "$SNAPSHOT"
