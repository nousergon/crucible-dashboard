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

# Mirror systemd/morning-signal.service.d/20-router.conf. Without these
# the recovery path declares a DIFFERENT execution context than the
# scheduled path (measured 2026-08-08: exec_context=laptop while running
# on EC2), and exec_context decides which registry entries may serve the
# caller — model-router-policy R29. A recovery run that resolves
# differently from the run it is recovering is not a recovery.
export KREPIS_EXEC_CONTEXT=ec2
export LLM_MODEL_REGISTRY_PATH=/home/ec2-user/alpha-engine-config/private-docs/LLM_MODEL_REGISTRY.yaml
export KREPIS_ROUTER_CREDENTIAL_SECRET=ROUTER_CONSUMER_MORNINGSIGNAL
# The AUTHENTICATED edge, not krepis's default http://127.0.0.1:8980 — the
# consumer credential above is translated into the router's own key at the
# edge, and the router process behind it has no database to look a virtual key
# up in, so the default URL and this credential cannot authenticate together.
# See 20-router.conf for the measured failure this prevents.
export KREPIS_LITELLM_PROXY_URL=https://router.nousergon.ai:8443
export PATH="/home/ec2-user/morning-signal/.venv/bin:/usr/local/bin:/usr/bin:/bin"

cd /home/ec2-user/morning-signal || { echo "ERROR: morning-signal checkout missing" >&2; exit 1; }

# Refresh to latest main (best-effort, like the service's ExecStartPre=-).
git fetch origin --quiet || echo "WARN: git fetch failed — running last-good code" >&2
git reset --hard origin/main || echo "WARN: git reset failed — running last-good code" >&2
/home/ec2-user/morning-signal/.venv/bin/python -m pip install -e . --quiet || echo "WARN: pip install failed — running last-good deps" >&2

echo "morning-signal-recover: generating (no daily-news sweep) ..."
exec /home/ec2-user/morning-signal/.venv/bin/python generate_episode.py generate "$@"
