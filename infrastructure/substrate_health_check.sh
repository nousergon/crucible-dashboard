#!/usr/bin/env bash
# substrate_health_check.sh — weekly-cadence transparency substrate health
# check invoked by WeeklySubstrateHealthCheck (ne-weekly-freshness-pipeline).
#
# alpha-engine-config-I7047 deliverable 1: extracts the three commands the
# SF state previously inlined behind a broken `trap 'aws s3 cp ... EXIT'`
# wrapper (`trap: s3: invalid signal specification`, rc=127 — the wrapper
# died before any check ran, on every Saturday run using it). The SF now
# invokes THIS script through krepis.ssm_log_capture, mirroring every
# other stage's `bash infrastructure/<script>.sh` shape (17 other Saturday
# SF stages already do this; these two health-observe stages were the only
# holdouts — I7047).
#
# Runs on the dashboard box (/home/ec2-user/alpha-engine-dashboard), via
# SSM AWS-RunShellScript. The SF's own command array does the `git pull`
# for both alpha-engine-dashboard and alpha-engine-data and `cd`s into
# alpha-engine-dashboard BEFORE invoking this script — this script does
# NOT re-pull, it only `cd`s between the two checked-out repos as each
# check needs.
#
# Deliberately carries NO Retry / keep-alive logic of its own (config#2279:
# declared class "health-observe", no Retry ladder by design — this is
# best-effort observability at the tail of an already-green run). The SF
# state's own Catch is the fail-soft path; this script's job is only to
# run the three checks in order and propagate the first non-zero exit.
set -eo pipefail

RUN_DATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-date)
      RUN_DATE="$2"
      shift 2
      ;;
    *)
      echo "substrate_health_check.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$RUN_DATE" ]]; then
  echo "substrate_health_check.sh: --run-date is required" >&2
  exit 2
fi

# Fleet-standard absolute venv interpreter (config#2954) — mirrors
# box_health.sh's VENV_PY / substrate_health_check_daily.sh's PYTHON_BIN.
# Do NOT `source .venv/bin/activate`: AL2023 carries no bare `python` on
# PATH outside a venv, and this venv's own `bin/python` symlink has gone
# missing in production before.
PYTHON_BIN=/home/ec2-user/alpha-engine-dashboard/.venv/bin/python

cd /home/ec2-user/alpha-engine-dashboard
"$PYTHON_BIN" -m nousergon_lib.transparency --cadence weekly --alert

echo "--- constituents drift check ---"
cd /home/ec2-user/alpha-engine-data
"$PYTHON_BIN" -m validators.constituents_drift_check

echo "--- phase marker sweep ---"
export RUN_DATE
"$PYTHON_BIN" -m validators.phase_marker_sweep --run-date "$RUN_DATE" --alert
