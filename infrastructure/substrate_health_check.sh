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

# Stage-coverage window (config-I7214): the instant this stage started.
# An artifact older than this is a leftover from a previous cycle, not this
# run's output — an existence-only probe cannot tell those apart.
_STAGE_WINDOW_START="${_STAGE_WINDOW_START:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

RUN_DATE=""
EXECUTION_ARN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-date)
      RUN_DATE="$2"
      shift 2
      ;;
    --execution-arn)
      # alpha-engine-config-I7167. OPTIONAL by design: this script is deployed
      # to the box by `git pull` and the SF state that passes this flag lands
      # in a separate repo (nousergon-data). Between the two merges the box
      # runs a script that accepts the flag from an SF that does not yet send
      # it — so it must degrade, not abort. Without it the stage-output sweep
      # cannot read the execution window or the entered-stage set and reports
      # `unmeasured` rather than inventing either.
      EXECUTION_ARN="$2"
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

# ── stage-output assertion (alpha-engine-config-I7167) ──────────────────────
#
# Asserts that every stage which ENTERED this execution wrote the S3 artifact
# it declares in ARTIFACT_REGISTRY.yaml's `produced_by:`. The 2026-08-08
# scheduled run terminated SUCCEEDED with five stages having produced nothing,
# and no surface reported it: a stage that runs, exits 0 and writes nothing was
# indistinguishable from one that did its job.
#
# Runs in OBSERVE mode — it alerts and writes its verdict to
# `_stage_outputs/{pipeline}/{run_date}.json`, and exits 0 regardless of what
# it finds. That is deliberate and load-bearing:
#
#   * this script runs under `set -eo pipefail` inside WeeklySubstrateHealthCheck,
#     whose States.ALL Catch sets $.degraded_summary;
#   * since alpha-engine-config-I6891 a degraded summary routes the run through
#     CheckDegradedOutcome -> WriteCompletionMarkerDegraded -> DegradedRun, a
#     **Fail** state;
#   * so any non-zero exit here terminates the whole ~4h weekly run as FAILED.
#
# Three of I7167's five instances are still open, so an enforcing default would
# hard-fail the next scheduled run for defects that predate the detector —
# `ruling_detect_before_enforcing_when_the_floor_is_unmeasured` (Brian,
# 2026-08-11). The first observe run publishes the real finding count; `--enforce`
# is the one-word flip once that floor is known and the residue triaged.
#
# `|| true` is NOT a silent swallow: the sweep already exits 0 for findings, so
# this only absorbs an unhandled crash of the sweep itself. Its recording
# surfaces are (a) this ERROR line, and (b) the registry row on the verdict key,
# which goes stale and pages when the sweep stops writing. Without it, a bug in
# a brand-new observability check could fail a four-hour production run — the
# reporter destroying the thing it reports.
echo "--- stage output sweep (observe) ---"
cd /home/ec2-user/alpha-engine-data
if ! "$PYTHON_BIN" -m validators.stage_output_sweep \
    --run-date "$RUN_DATE" \
    ${EXECUTION_ARN:+--execution-arn "$EXECUTION_ARN"}; then
  echo "substrate_health_check.sh: stage_output_sweep CRASHED (not a finding — the" \
       "sweep exits 0 for findings by design). The weekly run is NOT failed for" \
       "this; investigate via _stage_outputs/ and alpha-engine-config-I7167." >&2
fi

# Per-stage output assertion (config-I7214, sf-pipeline-policy.md §2.1):
# assert THIS stage wrote what it declared, at the boundary where the fact
# becomes knowable. OBSERVE MODE — it can never fail the stage.
#
# krepis, not nousergon_lib: krepis is the fleet's sanctioned bash/runpy
# entrypoint namespace (this script already calls `-m nousergon_lib.*` only
# for genuine non-shim modules like `transparency` — see
# tests/test_no_runpy_alias_invocation.py's `_REAL_NL_MODULE_EXEMPTIONS`).
# `stage_coverage` lands in nousergon_lib (nousergon-lib-PR314, merged 19:03:24Z). The
# runpy guard forbids the alpha_engine_lib ALIAS, not nousergon_lib itself — its
# own docstring names `-m nousergon_lib.alerts` as the CORRECT invocation.
"$PYTHON_BIN" -m nousergon_lib.stage_coverage assert --stage WeeklySubstrateHealthCheck --window-start "$_STAGE_WINDOW_START" || echo "WARNING: stage-coverage assertion did not run for WeeklySubstrateHealthCheck (rc=$?) — observe mode, stage NOT failed (config-I7214)" >&2
