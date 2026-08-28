#!/bin/bash
# git-sync-lock.sh — single source of truth for the per-checkout advisory
# flock every git-writing script on a shared on-box checkout must acquire
# before it fetches/resets/pulls that checkout (alpha-engine-config
# incident class, mirrors crucible-executor's infrastructure/lib/
# git-sync-lock.sh / $GIT_SYNC_LOCK, alpha-engine-config#1944).
#
# Source this file rather than hardcoding the lock path/wait constants —
# the mutex only serializes writers that flock the SAME inode, so a second
# copy of the literal is a second (silently non-cooperating) lock, not a
# harmless duplicate.
#
# Incident this closes (2026-08-27 20:07 UTC): this repo owns TWO shared
# on-box checkouts, each with multiple unsynchronised git writers and no
# lock at all —
#   - /home/ec2-user/alpha-engine-dashboard: .github/workflows/deploy.yml
#     (SSM, on merge), infrastructure/boot-pull.sh (daily timer),
#     infrastructure/substrate_health_check_daily.sh (Mon-Fri 22:30 UTC
#     health check).
#   - /home/ec2-user/morning-signal: morning-signal-pull.service,
#     morning-signal.service, morning-signal-bakeoff.service,
#     morning-signal-recover.sh (systemd + a manual recovery script — all
#     staggered only by a comment about time offsets, no actual lock).
# On 2026-08-27 20:07 UTC two of the writers on ~/metron (a sibling
# checkout, same class) collided: `git fetch`'s own ref update to
# refs/remotes/origin/main lost a compare-and-swap race —
# `error: cannot lock ref 'refs/remotes/origin/main': is at 95cd989 but
# expected 0f2a6b8` — before the deploy script even started, so its
# failure trap never fired and the commit sat undeployed for five hours.
#
# One lock PER CHECKOUT, not one lock for the whole box: unlike
# crucible-executor's trading-box writers (which share cross-repo
# dependencies and so share one box-wide lock), alpha-engine-dashboard and
# morning-signal are unrelated products with unrelated release cadences —
# serializing writers within each checkout is sufficient to close this
# incident class without adding needless cross-product contention.
#
# Lock lives under /tmp (not the checkout itself, and not a fixed
# per-repo literal): every actor flocks a path derived from the checkout's
# own basename, so a NEW shared checkout gets a correctly-scoped lock for
# free just by calling git_sync_lock_path on its own directory, with no
# third literal to keep in sync.
GIT_SYNC_LOCK_WAIT="${AE_GIT_SYNC_LOCK_WAIT:-150}"

# git_sync_lock_path <checkout-dir> — echoes the lock path for that
# checkout. Callers capture it once per checkout, e.g.:
#   GIT_SYNC_LOCK="$(git_sync_lock_path /home/ec2-user/morning-signal)"
#   flock -w "$GIT_SYNC_LOCK_WAIT" "$GIT_SYNC_LOCK" git fetch origin
git_sync_lock_path() {
    local checkout_dir="$1"
    local base
    base="$(basename "$checkout_dir")"
    echo "/tmp/nousergon-git-sync-${base}.lock"
}

# git_sync_state_path <checkout-dir> — echoes the path of the small state
# file a checkout's sync script records its OUTCOME to (alpha-engine-config
# incident: `ExecStartPre=-morning-signal-sync.sh` is deliberately
# failure-tolerant, so a failed sync starts the service on whatever code was
# already on disk and the unit still exits 0 — nothing said the run was
# stale). The sync script is the only writer; a drift check (box_health.sh's
# check_morning_signal_sync_drift) is the reader. Same derivation as
# git_sync_lock_path — one basename-keyed literal, not a second copy of it —
# so a new shared checkout's sync script gets a correctly-scoped state path
# for free.
#
# `.json`-suffixed for readability only: the format written is plain
# `KEY=value` lines (SYNC_OK, HEAD_SHA, SYNCED_AT), not actual JSON, so it
# can be read with awk/grep in a `set -u` bash without a JSON parser
# dependency on the box.
git_sync_state_path() {
    local checkout_dir="$1"
    local base
    base="$(basename "$checkout_dir")"
    echo "/tmp/nousergon-git-sync-state-${base}.json"
}
