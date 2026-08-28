#!/bin/bash
# morning-signal-recover.sh — Generate-only recovery for a missed/failed episode.
#
# Re-runs JUST the podcast generation (refresh code -> generate -> publish)
# against the EXISTING news digest, WITHOUT triggering daily-news.service.
#
# Why this exists (morning-signal#78): the scheduled path is
# `morning-signal.service` whose drop-in pulls in daily-news (a full-universe
# news sweep) first. Re-triggering that service to recover an episode re-runs
# the sweep — slow, and repeated same-day runs trip GDELT's rate limit (429),
# making recovery crawl. The podcast does NOT need a fresh digest to recover;
# this wrapper skips daily-news entirely.
#
# Mirrors morning-signal.service's Environment + ExecStartPre + ExecStart.
# Run as the ec2-user (it assumes the runner role via SSM identically):
#   sudo -u ec2-user bash /usr/local/bin/morning-signal-recover.sh [generate-args]

set -uo pipefail

export MORNING_SIGNAL_RUNNER_ROLE_ARN="arn:aws:iam::711398986525:role/morning-signal-runner-role"
export MORNING_SIGNAL_USE_SSM=1
export MORNING_SIGNAL_SSM_REGION=us-east-1

# SOURCE the same file the units read, rather than mirroring it. Without the
# router contract the recovery path declares a DIFFERENT execution context than
# the scheduled path (measured 2026-08-08: exec_context=laptop while running on
# EC2), and exec_context decides which registry entries may serve the caller —
# model-router-policy R29. A recovery run that resolves differently from the
# run it is recovering is not a recovery.
#
# Mirroring is what produced the 2026-08-12 state: this file and the drop-in
# agreed, and `morning-signal-bakeoff.service` — the third reader — had none of
# it. One file cannot be two-thirds applied.
#
# FATAL if absent, not best-effort. Continuing without it would resolve against
# a default context with no credential and quietly produce exactly the split
# this change removes.
ROUTER_ENV=/etc/morning-signal/router-env.conf
if [ ! -r "$ROUTER_ENV" ]; then
    echo "ERROR: $ROUTER_ENV missing or unreadable — run install-morning-signal.sh." >&2
    echo "       Refusing to run: without it this recovery resolves a different" >&2
    echo "       router than the run it is recovering." >&2
    exit 1
fi
set -a
# shellcheck source=/dev/null
. "$ROUTER_ENV"
set +a
export PATH="/home/ec2-user/morning-signal/.venv/bin:/usr/local/bin:/usr/bin:/bin"

cd /home/ec2-user/morning-signal || { echo "ERROR: morning-signal checkout missing" >&2; exit 1; }

# Refresh to latest main (best-effort, like the service's ExecStartPre=-).
# Routed through the shared sync script (flock + fetch retry + origin/main
# presence check — alpha-engine-config incident 2026-08-27 20:07 UTC, see
# infrastructure/lib/git-sync-lock.sh) rather than the bare git commands
# this wrapper used to run directly — this file WAS the third,
# non-cooperating writer against this checkout the incident's sweep found.
/usr/local/bin/morning-signal-sync.sh /home/ec2-user/morning-signal || echo "WARN: morning-signal-sync failed — running last-good code" >&2
/home/ec2-user/morning-signal/.venv/bin/python -m pip install -e . --quiet || echo "WARN: pip install failed — running last-good deps" >&2

echo "morning-signal-recover: generating (no daily-news sweep) ..."
exec /home/ec2-user/morning-signal/.venv/bin/python generate_episode.py generate "$@"
