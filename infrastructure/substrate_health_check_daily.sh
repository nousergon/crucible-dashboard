#!/usr/bin/env bash
# substrate_health_check_daily.sh — daily-cadence transparency substrate
# health check, re-homed off ne-postclose-trading-pipeline's
# DailySubstrateHealthCheck chain onto a standalone systemd timer
# (alpha-engine-config-I2722). The 7-state SF chain (skip-gate, task,
# poll-wait loop, status choice, degraded pass, best-effort SNS alert) is
# removed from infrastructure/step_function_eod.json in nousergon-data;
# this script preserves the SAME command sequence the SF's
# DailySubstrateHealthCheck Task ran via SSM (AWS-RunShellScript),
# unchanged in substance (the SF ran `sudo -u ec2-user git ... pull`
# because SSM's AWS-RunShellScript document runs as root; this script
# drops that prefix because the systemd service itself already runs as
# User=ec2-user — same effective user, no behavior change).
#
# Per-row CloudWatch metrics (AlphaEngine/Substrate) + existing alarms
# already carry the alerting independently of any orchestrator — this
# script's own exit code / journal / shipped log is a secondary
# observability surface, same as it was as an SF Task.
set -eo pipefail

# ── Wait for the post-close RECONCILE to FINISH (alpha-engine-config-I11581, I11269) ──
#
# The daily rows this checks (`pnl_attribution` from EOD reconcile,
# `trade_execution_lineage`, `risk_events`, `data_quality`) are written by the
# EOD reconcile. The timer used to fire at a fixed 22:30 UTC on the belief that
# postclose "normally completes" by then. The decoupled data cutover
# (nousergon-data#1930) made the reconcile wait for ne-data-collection-eod
# (18:15 ET, derived worst case 20:55 ET, I11363), so it now ends around
# 20:30 ET — after the old fire, which would grade the PREVIOUS cycle's rows
# every day.
#
# So this waits on a completion marker, written by WriteCompletionMarkerNormal /
# WriteCompletionMarkerDegraded immediately before a success terminal, keyed by
# the pipeline's `run_date` — the New York trading date, not the box's UTC date.
#
# WHICH marker depends on which definition is deployed (nousergon-data-PR1996):
#
#   - SPLIT (after PR1996 deploys): the reconcile lives in its own machine,
#     ne-postclose-reconcile-pipeline, and ONLY its marker means the rows are
#     written. ne-postclose-trading-pipeline's marker now lands ~16:20 ET,
#     BEFORE the reconcile — accepting it would grade the previous cycle again.
#   - PRE-SPLIT (before PR1996 deploys): no reconcile machine exists, and
#     ne-postclose-trading-pipeline's marker lands after its own EODReconcile.
#
# The two markers have identical bodies, so the discriminator is the deployed
# post-close DEFINITION itself: nousergon-data's deploy-infrastructure.sh
# uploads exactly the bytes it deploys to s3://alpha-engine-research/
# infrastructure/step_function_eod.json (contract sf_definition_s3-1.0.0). The
# post-close marker is accepted ONLY while that definition still contains an
# `EODReconcile` state. Anything else — split deployed, object unreadable,
# probe fault — requires the reconcile marker, so the failure direction is
# "later", never "grades pre-reconcile rows". Re-read every poll, so a deploy
# mid-wait is honoured. Once the split is verified live, the pre-split branch
# is dead and is removed (alpha-engine-config-I11269 follow-up).
#
# The wait is BOUNDED: at WAIT_UNTIL_ET the check runs anyway against whatever
# is published and says so, because a reconcile that never finished pages from
# its own HandleFailure and the per-row staleness this check measures is
# exactly the evidence an operator then needs. 23:30 ET keeps the whole wait
# inside one New York date. A holiday runs out the wait and checks, as before.
RUN_DATE="$(TZ=America/New_York date +%F)"
RECONCILE_MARKER="_sf_completion/ne-postclose-reconcile-pipeline/${RUN_DATE}.json"
PRESPLIT_POSTCLOSE_MARKER="_sf_completion/ne-postclose-trading-pipeline/${RUN_DATE}.json"
POSTCLOSE_DEFINITION_KEY="infrastructure/step_function_eod.json"
WAIT_UNTIL_ET="${SUBSTRATE_HEALTH_WAIT_UNTIL_ET:-23:30}"
POLL_SECONDS=300
WAIT_DEADLINE_EPOCH="$(TZ=America/New_York date -d "$RUN_DATE $WAIT_UNTIL_ET" +%s)"

# 0 = the marker is present. 1 = absent (404). 2 = probe fault, printed so a
# permissions or network problem is not read as a slow pipeline.
probe_marker() {
  local key="$1" err
  if err="$(aws s3api head-object --bucket alpha-engine-research --key "$key" 2>&1 >/dev/null)"; then
    return 0
  fi
  case "$err" in
    *"Not Found"*|*"(404)"*) return 1 ;;
    *) echo "completion probe FAULT on $key (still waiting): $err" >&2; return 2 ;;
  esac
}

# True only on positive evidence that the deployed post-close definition still
# runs the reconcile itself (the pre-split shape).
# Read into a variable, never piped straight into `grep -q`: under pipefail,
# grep exiting on its first match SIGPIPEs the download, and the pipeline would
# report failure on exactly the input that matched.
postclose_is_presplit() {
  local definition
  definition="$(aws s3 cp "s3://alpha-engine-research/$POSTCLOSE_DEFINITION_KEY" - 2>/dev/null)" || return 1
  grep -q '"EODReconcile"[[:space:]]*:' <<<"$definition"
}

while true; do
  if probe_marker "$RECONCILE_MARKER"; then
    echo "reconcile completion marker present: s3://alpha-engine-research/$RECONCILE_MARKER"
    break
  fi
  if postclose_is_presplit && probe_marker "$PRESPLIT_POSTCLOSE_MARKER"; then
    echo "post-close split not deployed yet (s3://alpha-engine-research/$POSTCLOSE_DEFINITION_KEY" \
         "still defines EODReconcile); pre-split postclose completion marker present:" \
         "s3://alpha-engine-research/$PRESPLIT_POSTCLOSE_MARKER"
    break
  fi
  if [ "$(date +%s)" -ge "$WAIT_DEADLINE_EPOCH" ]; then
    echo "WARNING: no reconcile completion marker for $RUN_DATE by $WAIT_UNTIL_ET ET" \
         "(s3://alpha-engine-research/$RECONCILE_MARKER) — checking the rows as published." >&2
    break
  fi
  sleep "$POLL_SECONDS"
done

CHECKOUT_DIR=/home/ec2-user/alpha-engine-dashboard
cd "$CHECKOUT_DIR"

# shellcheck source=lib/git-sync-lock.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/git-sync-lock.sh"
GIT_SYNC_LOCK="$(git_sync_lock_path "$CHECKOUT_DIR")"

# ff-only, matching the SF's `git ... pull --ff-only origin main` exactly
# (no reset --hard embellishment) — a diverged checkout should fail loud
# here rather than silently rewrite history on a box other services share.
#
# Flocked (config incident 2026-08-27 20:07 UTC, see infrastructure/lib/
# git-sync-lock.sh): this health check is one of several unsynchronised
# writers against this SAME checkout (deploy.yml, boot-pull.sh also touch
# it), and a pull's own ref update can lose a compare-and-swap race
# against a concurrent writer just like a bare fetch can.
flock -w "$GIT_SYNC_LOCK_WAIT" "$GIT_SYNC_LOCK" git pull --ff-only origin main

# Fleet-standard absolute venv interpreter (config#2954) — mirrors
# box_health.sh's VENV_PY / morning-signal-watchdog.sh's DASH_PY. `source
# .venv/bin/activate` alone is not sufficient: AL2023 carries no bare
# `python` symlink on PATH outside a venv, and this venv's own `bin/python`
# symlink has gone missing at least once in production (the `python:
# command not found` failure this fixes) — the absolute path removes the
# dependency on activation having produced a working `python` at all.
PYTHON_BIN=/home/ec2-user/alpha-engine-dashboard/.venv/bin/python

# systemd LogsDirectory=substrate-health-daily (see the .service unit)
# creates this directory pre-owned by the service's User=/Group= before
# ExecStart runs — /var/log/ itself is root-owned and not writable by
# ec2-user, which is what made the old direct /var/log/*.log path fail.
LOG_FILE=/var/log/substrate-health-daily/run.log

# Ship the run log to S3 on exit — same trap the SF's Task ran inline.
trap 'aws s3 cp "$LOG_FILE" "s3://alpha-engine-research/_ssm_logs/substrate-health-check-daily/$(date -u +%Y-%m-%d)/$(hostname)-$(date -u +%H%M%SZ).log" --only-show-errors || true' EXIT

"$PYTHON_BIN" -m nousergon_lib.transparency --cadence daily --alert 2>&1 | tee "$LOG_FILE"
