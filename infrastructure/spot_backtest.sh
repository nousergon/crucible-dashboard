#!/usr/bin/env bash
# infrastructure/spot_backtest.sh — Run weekly backtest on a spot EC2 instance.
#
# Launches a c5.large spot instance (~$0.03/hr), clones the backtester +
# predictor + executor repos, runs the full backtest pipeline with 10y of
# price data, uploads results to S3, and self-terminates.
#
# Usage:
#   ./infrastructure/spot_backtest.sh                   # full run (--mode all)
#   ./infrastructure/spot_backtest.sh --smoke-only      # quick validation, then terminate
#   ./infrastructure/spot_backtest.sh --preflight-only  # boot + deps + the
#                                                       #   bootstrap-class smoke
#                                                       #   harness only
#                                                       #   (backtest.py --mode=smoke:
#                                                       #   BacktesterPreflight +
#                                                       #   _runtime_smoke — lib-pin /
#                                                       #   imports / predictor-weights /
#                                                       #   universe-freshness, ~30-60s,
#                                                       #   from PRs #43-#48), then
#                                                       #   exit 0 — NO param sweep,
#                                                       #   NO portfolio sim, NO parity,
#                                                       #   NO evaluator, NO config/*.json
#                                                       #   auto-apply, ZERO external API
#                                                       #   calls, ZERO S3/config writes.
#                                                       #   Friday shell_run dry path
#                                                       #   (ROADMAP "Friday shell-run —
#                                                       #   per-module dry-path
#                                                       #   activation" owed-item #3).
#   ./infrastructure/spot_backtest.sh --mode simulate   # override backtest mode
#   ./infrastructure/spot_backtest.sh --instance-type c5.xlarge  # override instance type
#   ./infrastructure/spot_backtest.sh --dry-run         # full-universe exercise without
#                                                       #   production S3 pollution:
#                                                       #   markers + artifacts + reports
#                                                       #   go to .dry-run/{date}/, no
#                                                       #   optimizer config writes, no
#                                                       #   reporter upload. Safe to run
#                                                       #   concurrently with scheduled SF.
#   ./infrastructure/spot_backtest.sh --use-vectorized-sweep  # run predictor_param_sweep
#                                                       #   through the matrix-axis vectorized
#                                                       #   engine (Tier 4). Default off until
#                                                       #   v14 spot validation confirms parity.
#
# Prerequisites:
#   - AWS CLI with perms to RunInstances / TerminateInstances /
#     DescribeInstances / SendCommand / GetCommandInvocation
#   - alpha-engine-lib installed in the dispatcher venv (ec2_spot +
#     ssm_dispatcher CLIs); LIB_PYTHON points at it
#   - Code committed and pushed to origin (instance clones from GitHub
#     via HTTPS — no SSH key needed)
#   - config.yaml + executor risk.yaml + predictor predictor.yaml
#     (gitignored — staged to S3 by this script for the spot to fetch
#     via its alpha-engine-executor-profile IAM role). config.yaml carries
#     the non-secret runtime config (EMAIL_SENDER, EMAIL_RECIPIENTS,
#     OUTPUT_BUCKET) the .env used to hold (#890 deprecated the .env).
#
# **2026-05-27 — SSH/SCP → SSM transport migration (ROADMAP L342 PR 3).**
# Mirrors alpha-engine-data PR 2 (#330). Communication with the spot is
# now via `aws ssm send-command` wrapped at the lib chokepoint
# `python -m krepis.ssm_dispatcher run` (invoked directly via krepis per
# config#1649 — the nousergon_lib re-export shim is guard-less under
# `python -m` on lib >=0.81.0 and silently no-ops). No port-22 inbound on
# the spot SG; no ssh / scp / ssh-keyscan. The 3 config files (no .env post
# #890) are staged to a temporary S3 prefix and pulled down by the spot. PR 3 of
# the 5-PR L342 arc.
#
# For scheduled weekly runs, call this script from the always-on EC2 cron
# or from an EventBridge → Lambda trigger:
#
#   0 8 * * 1  cd ~/alpha-engine-backtester && bash infrastructure/spot_backtest.sh >> /var/log/backtester-spot.log 2>&1

set -euo pipefail

# ── Ensure HOME is set (SSM RunCommand does not set it) ──────────────────────
export HOME="${HOME:-/home/ec2-user}"

# ── Path setup ───────────────────────────────────────────────────────────────
# .env fully deprecated (#890). Secrets load from SSM via
# alpha_engine_lib.secrets.get_secret() at Python startup (the EC2 instance
# role grants ssm:GetParameter on /alpha-engine/*). The remaining non-secret
# runtime config the .env used to carry (EMAIL_SENDER, EMAIL_RECIPIENTS,
# OUTPUT_BUCKET) now lives in config.yaml — already staged to S3 and fetched
# by the spot — so no .env is staged, fetched, or sourced anywhere below.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# config#903: this launcher now lives in the crucible-dashboard (dispatcher)
# checkout, so SCRIPT_DIR/.. is the dashboard repo — NOT the crucible-backtester
# checkout whose config.yaml / backtest.py / evaluate.py / preflight.py /
# requirements.txt this launcher reads for pre-launch validation + S3 staging.
# The SF SSM command sets REPO_ROOT=/home/ec2-user/alpha-engine-backtester before
# invoking this script; when REPO_ROOT is unset (a manual run from inside the
# backtester checkout) it falls back to the historical SCRIPT_DIR/.. so behaviour
# is byte-identical to the pre-relocation script for the co-located case.
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# ── Configuration ──────────────────────────────────────────────────────────────
AWS_REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-alpha-engine-research}"
BRANCH="${BRANCH:-main}"
# Capacity-resilient instance-type fallback set (2026-05-22 incident:
# THIS LAUNCHER's Evaluator invocation hit InsufficientInstanceCapacity
# for c5.large in subnet-e07166ec / us-east-1f). All 2 vCPU / 4-8 GB
# RAM — equivalent for the backtester (memory-bound; the 2026-04-23
# predictor_data_prep OOM is being fixed structurally via the
# ohlcv_by_ticker → DataFrame refactor — P2 in SYSTEM_STATE).
INSTANCE_TYPES="${INSTANCE_TYPES:-c5.large,m5.large,c6i.large,c5a.large}"
INSTANCE_TYPE=""  # backward-compat: --instance-type X collapses INSTANCE_TYPES to single value
AMI_ID="ami-0c421724a94bba6d6"      # Amazon Linux 2023 x86_64
# Spot-side watchdog budget: backtester's 10y simulate + param sweep
# historically runs 60-100 min. 120 min with headroom. Bump (don't
# silently rely on the orphan reaper) if a run legitimately needs more.
MAX_RUNTIME_SECONDS="${MAX_RUNTIME_SECONDS:-7200}"
# KEY_NAME kept ONLY as launch attribute for alpha_engine_lib.ec2_spot's
# --key-name flag — the spot still launches with the key associated, but
# NOTHING in this script SSHs in. Communication is via SSM. KEY_FILE was
# removed in the 2026-05-27 SSH→SSM migration (PR 3 of L342); manual
# break-glass SSH is possible only by temporarily re-opening the SG's
# port-22 inbound (which it should NOT be in steady state).
KEY_NAME="alpha-engine-key"
SECURITY_GROUP="sg-03cd3c4bd91e610b0"
# All 6 default-VPC subnets across us-east-1{a..f}. The lib CLI rotates
# across this list on capacity error. Lockstep with data + predictor
# launchers (same VPC vpc-566f002e, same SG).
SUBNETS="${SUBNETS:-subnet-a61ec0fb,subnet-1e58307a,subnet-789d3857,subnet-c670118d,subnet-7cff7c43,subnet-e07166ec}"
IAM_PROFILE="alpha-engine-executor-profile"
# Lib CLI path: ae-dashboard is the SSM target for Backtester / Parity
# / Evaluator states; the dispatcher's .venv has alpha-engine-lib
# installed.
LIB_PYTHON="${LIB_PYTHON:-/home/ec2-user/alpha-engine-dashboard/.venv/bin/python}"
BACKTEST_MODE="all"

# ── Parse flags ──────────────────────────────────────────────────────────────
RUN_MODE="full"  # full | smoke-only
# PREFLIGHT_ONLY is a MODIFIER, orthogonal to RUN_MODE — matching the
# data (spot_data_weekly.sh #259) and predictor (spot_train.sh #175)
# siblings' verbatim --preflight-only flag for cross-script consistency
# (the Friday shell_run SF keystone follow-on dispatches the same flag
# name to every module). When set, the script boots + installs deps for
# real, runs ONLY the bootstrap-class smoke harness (backtest.py
# --mode=smoke = BacktesterPreflight + _runtime_smoke; ~30-60s,
# read-only), then `exit 0` BEFORE the per-phase smoke modes, the
# evaluate.py S3-probe diagnostics, AND the entire full-backtest heredoc
# (param sweep / portfolio sim / parity / pit_parity / evaluator /
# config/*.json optimizer auto-apply / CloudWatch heartbeats). Catches
# bootstrap-class breakage (lib-pin drift, sys.path collision, stale
# ArcticDB universe, missing predictor weights, SSM timeout, image gap)
# ~12h before the real Saturday Backtester. backtest.py --mode=smoke
# itself `return`s before _init_pipeline / the optimizer, so it writes
# no S3 config; gating in front of the full heredoc + the
# evaluate.py/per-phase smoke block makes every sweep/sim/parity/
# evaluator and every config/{executor,scoring,predictor,research,
# scanner}_params*.json writer statically unreachable under this flag.
PREFLIGHT_ONLY=0
# All PhaseRegistry-adjacent flags are also routable from the
# Saturday SF input via env vars. When set they pass through as
# CLI args to backtest.py.
SKIP_PHASE4="${SKIP_PHASE4_EVALUATIONS:-false}"
SKIP_PHASES="${SKIP_PHASES:-}"            # comma-separated phase names
ONLY_PHASES="${ONLY_PHASES:-}"            # comma-separated phase names
FORCE_ALL="${FORCE_ALL:-false}"           # true → --force
FORCE_PHASES="${FORCE_PHASES:-}"          # comma-separated phase names
DRY_RUN="${DRY_RUN:-false}"               # true → --dry-run
# Pipeline-level stage control: comma-separated subset of {backtest, parity,
# evaluator}. All three stages run by default on the spot. Used for fast
# iteration against a single stage (e.g. parity-only when debugging a cred
# divergence).
SKIP_STAGES="${SKIP_STAGES:-}"
# pit_parity observational stage (ROADMAP L2371 / plan §D4). DEFAULT ON
# 2026-05-17 (Brian): every Saturday SF spot run now emits
# backtest/{date}/pit_parity.json (the skilled-risk-basket contamination
# report). NON-BLOCKING + writes no configs + does NOT flip --walk-forward
# (the L2371 close is the separate, manual, post-review step). Opt out per
# run with --no-pit-parity or PIT_PARITY_ENABLED=0 (ad-hoc/dry iterations
# where the extra predictor-sim pass isn't wanted).
PIT_PARITY_ENABLED="${PIT_PARITY_ENABLED:-1}"
# RUN_DATE: the single artifact-date label for backtest/{date}/ (param
# sweep + portfolio_stats + parity + pit_parity + evaluator inputs). The
# Saturday SF stamps this ONCE at InitializeInput from
# $$.Execution.StartTime and threads it (export RUN_DATE=…) into the
# Backtester / Parity / Evaluator SSM commands — each a SEPARATE spot
# instance with its own spot_backtest.sh invocation. Resolving it here
# from the injected env (not per-stage wall-clock) is what keeps all
# three stages keyed to the SAME prefix when a multi-hour run straddles
# UTC midnight (the 2026-05-17 Evaluator failure: Backtester wrote
# backtest/2026-05-17/, Evaluator looked in backtest/2026-05-18/). Same
# dispatcher→heredoc bake-in mechanism as SKIP_STAGES / PIT_PARITY_ENABLED.
# Falls back to wall-clock UTC for ad-hoc manual runs that don't inject it.
RUN_DATE="${RUN_DATE:-$(date -u +%Y-%m-%d)}"
# DATE_CONVENTIONS: normalize RUN_DATE to the NYSE TRADING DAY at this single
# dispatcher-side chokepoint, BEFORE it is threaded into every stage's --date
# AND the bash s3 uploads below (so python + bash never split). The SF threads
# $.run_date = date(Execution.StartTime) (CALENDAR — Sat 2026-05-30 on a
# Saturday firing) but Research + signals.json + the standalone scanner key by
# trading day (Fri 2026-05-29); keying backtest/{date}/ (incl. pit_parity.json
# + parity_metrics) by the calendar date is what surfaced the research↔backtester
# pit-parity drift (L4466). $LIB_PYTHON (line ~125) carries nousergon_lib.
# Defensive: keep the calendar value if the lib call fails (a normalization
# miss must not abort the backtester) — the python entry points re-normalize
# idempotently as a backstop.
_RUN_DATE_TD="$("$LIB_PYTHON" -c "import datetime as d; from nousergon_lib import trading_calendar as tc; x=d.date.fromisoformat('${RUN_DATE}'[:10]); print(x.isoformat() if tc.is_trading_day(x) else tc.previous_trading_day(x).isoformat())" 2>/dev/null || true)"
if [ -n "$_RUN_DATE_TD" ]; then
    if [ "$_RUN_DATE_TD" != "$RUN_DATE" ]; then
        echo "==> Normalized RUN_DATE ${RUN_DATE} (calendar) → ${_RUN_DATE_TD} (trading day) per DATE_CONVENTIONS"
    fi
    RUN_DATE="$_RUN_DATE_TD"
else
    echo "WARNING: trading-day normalization of RUN_DATE=${RUN_DATE} failed — keeping calendar value (python entry points will re-normalize)" >&2
fi
# Freeze the evaluator (passes --freeze to evaluate.py → suppresses per-
# optimizer S3 config writes; report artifacts + email still upload). Use
# for off-cycle test runs so mid-week sweeps don't auto-promote weights/
# params/thresholds against Monday trading. Replaces the retired SF
# CheckEvaluatorFreeze Choice state (evaluator consolidated into spot
# 2026-04-24); the freeze_evaluator SF input param is no longer honored.
FREEZE_EVALUATOR="${FREEZE_EVALUATOR:-false}"
USE_VECTORIZED_SWEEP="${USE_VECTORIZED_SWEEP:-false}"
# Accept both --flag value and --flag=value forms for every value-taking
# flag. The equals form is GNU-getopt-style muscle memory and it's cheap to
# support — each value flag gets a companion `--foo=*` case that splits on
# `=`. Boolean flags (--smoke-only, --force, --dry-run, etc.) accept no
# value and don't need the companion case.

# L4485-b (2026-06-05): bounded self-relaunch on a mid-run AWS spot RECLAIM.
# The Saturday SF's per-state Retry is on the `ssm:sendCommand` Task, which
# only SENDS the command and returns — the actual run is polled by a separate
# Choice loop, so a worker spot reclaimed mid-run (Server.SpotInstanceTermination
# / instance-terminated-no-capacity) surfaces in the poll as a generic Failed,
# NOT a sendCommand TaskFailed → the SF Retry never fires (its "handles spot
# interruption" comment is structurally wrong). This dispatcher (which OWNS the
# worker-spot lifecycle) is the only layer that can see the reclaim reason — it
# already classifies it in cleanup(). On a classified reclaim, cleanup() re-execs
# this script on a FRESH spot, bounded by RECLAIM_RELAUNCH_MAX. Gated STRICTLY on
# the reclaim reason — any non-reclaim failure exits immediately (no blind retry
# that could mask a real bug, per feedback_no_silent_fails). Happy path unchanged.
# Default budget 3 (was 1): 2026-06-06 saw TWO consecutive reclaims during a
# capacity-volatile window; a budget of 1 exhausts on such a streak. Each
# relaunch resumes cheaply via the S3 phase auto-skip markers (completed phases
# are skipped on the fresh spot), so a higher bound is low-cost and only ever
# burns on a CLASSIFIED reclaim — a real crash/OOM/timeout still exits at once.
RECLAIM_RELAUNCH_MAX="${RECLAIM_RELAUNCH_MAX:-3}"
_ORIG_ARGS=("$@")  # captured pre-parse for the relaunch exec

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smoke-only) RUN_MODE="smoke-only"; shift ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
        --instance-type=*) INSTANCE_TYPE="${1#*=}"; shift ;;
        --mode) BACKTEST_MODE="$2"; shift 2 ;;
        --mode=*) BACKTEST_MODE="${1#*=}"; shift ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --branch=*) BRANCH="${1#*=}"; shift ;;
        --skip-phase4-evaluations) SKIP_PHASE4="true"; shift ;;
        --skip-phases) SKIP_PHASES="$2"; shift 2 ;;
        --skip-phases=*) SKIP_PHASES="${1#*=}"; shift ;;
        --only-phases) ONLY_PHASES="$2"; shift 2 ;;
        --only-phases=*) ONLY_PHASES="${1#*=}"; shift ;;
        --force) FORCE_ALL="true"; shift ;;
        --force-phases) FORCE_PHASES="$2"; shift 2 ;;
        --force-phases=*) FORCE_PHASES="${1#*=}"; shift ;;
        --dry-run) DRY_RUN="true"; shift ;;
        --skip-stages) SKIP_STAGES="$2"; shift 2 ;;
        --skip-stages=*) SKIP_STAGES="${1#*=}"; shift ;;
        --no-pit-parity) PIT_PARITY_ENABLED="0"; shift ;;
        --pit-parity-enabled) PIT_PARITY_ENABLED="$2"; shift 2 ;;
        --pit-parity-enabled=*) PIT_PARITY_ENABLED="${1#*=}"; shift ;;
        --run-date) RUN_DATE="$2"; shift 2 ;;
        --run-date=*) RUN_DATE="${1#*=}"; shift ;;
        --freeze-evaluator) FREEZE_EVALUATOR="true"; shift ;;
        --use-vectorized-sweep) USE_VECTORIZED_SWEEP="true"; shift ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

# ── Validate --skip-stages against the known stage vocabulary ────────────────
# Hard-fail on unknown names per no-silent-fails: a typo like
# --skip-stages=evaulator would silently run evaluator (no match) and mislead
# the operator into thinking the pipeline respected their request.
_KNOWN_STAGES="backtest pit_parity parity evaluator"
if [ -n "$SKIP_STAGES" ]; then
    IFS=',' read -ra _SKIP_ARR <<< "$SKIP_STAGES"
    for _s in "${_SKIP_ARR[@]}"; do
        _s_trim="$(echo "$_s" | tr -d '[:space:]')"
        case " $_KNOWN_STAGES " in
            *" $_s_trim "*) ;;
            *)
                echo "ERROR: unknown stage '$_s_trim' in --skip-stages=$SKIP_STAGES" >&2
                echo "       Valid stages: $_KNOWN_STAGES" >&2
                exit 1
                ;;
        esac
    done
fi

# Convert each flag to a backtest.py CLI arg suffix (empty string when
# disabled, so we don't pass an invalid empty arg through the heredoc).
if [ "$SKIP_PHASE4" = "true" ]; then
    BACKTEST_SKIP_PHASE4_FLAG="--skip-phase4-evaluations"
else
    BACKTEST_SKIP_PHASE4_FLAG=""
fi

BACKTEST_PHASE_FLAGS=""
if [ -n "$SKIP_PHASES" ]; then
    BACKTEST_PHASE_FLAGS="$BACKTEST_PHASE_FLAGS --skip-phases=$SKIP_PHASES"
fi
if [ -n "$ONLY_PHASES" ]; then
    BACKTEST_PHASE_FLAGS="$BACKTEST_PHASE_FLAGS --only-phases=$ONLY_PHASES"
fi
if [ "$FORCE_ALL" = "true" ]; then
    BACKTEST_PHASE_FLAGS="$BACKTEST_PHASE_FLAGS --force"
fi
if [ -n "$FORCE_PHASES" ]; then
    BACKTEST_PHASE_FLAGS="$BACKTEST_PHASE_FLAGS --force-phases=$FORCE_PHASES"
fi
if [ "$DRY_RUN" = "true" ]; then
    BACKTEST_PHASE_FLAGS="$BACKTEST_PHASE_FLAGS --dry-run"
fi
if [ "$USE_VECTORIZED_SWEEP" = "true" ]; then
    BACKTEST_PHASE_FLAGS="$BACKTEST_PHASE_FLAGS --use-vectorized-sweep"
fi

# Smoke-safe subset of BACKTEST_PHASE_FLAGS. Smoke modes set their own
# only-/skip-phases via `_apply_smoke_fixture`, so propagating the
# operator's --skip-phases / --only-phases / --force-phases would
# conflict with the fixture's narrowing semantics. Only flags that
# affect compute behavior (not phase selection) flow through. Currently
# just --use-vectorized-sweep — added for Tier 4 Layer 2 smoke
# validation (ROADMAP P0 2026-04-27). Without this, the host parses
# --use-vectorized-sweep but no smoke command ever sees the flag, so
# `smoke-predictor-param-sweep` would silently exercise the scalar path.
SMOKE_PHASE_FLAGS=""
if [ "$USE_VECTORIZED_SWEEP" = "true" ]; then
    SMOKE_PHASE_FLAGS="$SMOKE_PHASE_FLAGS --use-vectorized-sweep"
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Backtester Spot Run — $(date +%Y-%m-%d)"
echo "═══════════════════════════════════════════════════════════════"

# ── Phase-aware instance-type floor (L4485) ──────────────────────────────────
# Modes that run predictor_pipeline (10y GBM inference over ~900 tickers;
# peak RSS ~2.8 GB measured 2026-06-01) need ≥8 GB RAM. The 4 GB c5.large —
# FIRST in the default rotation — OOM-killed predictor_pipeline on the
# 2026-06-01 off-cycle run. CRITICAL: the Saturday SF's PredictorBacktest +
# PortfolioOptimizerBacktest states invoke this script with NO --instance-type,
# so without this floor they inherit the c5.large-first default and OOM
# identically on the next weekly cycle (the operator's "why would Saturday
# succeed?" — it wouldn't). Setting the floor HERE fixes both the off-cycle
# --mode=all path and the SF split-states with zero edits to the Step Function.
# param-sweep / simulate / signal-quality don't load the predictor tensor and
# stay on the cheap 4 GB-first rotation. Skipped when the operator passes an
# explicit --instance-type (their choice wins, incl. deliberate small debug).
_PREDICTOR_RAM_FLOOR_TYPES="m5.large,m6i.large,m5a.large,c5.xlarge,c6i.xlarge"
# L4487 (2026-06-05): the ≥16 GB pit_parity floor (L4486d) is REVERTED to ≥8 GB.
# pit_parity now runs its two passes in separate subprocesses
# (analysis/pit_parity.py::_run_predictor_pass_isolated → backtest.py
# --pit-parity-pass), so the OS reclaims each pass's RSS between passes — the
# Parity spot's footprint is bounded to ONE pass (~2.8 GB), which fits the 8 GB
# floor with margin. No PIT_PARITY_ENABLED special-case: all predictor-bearing
# modes (incl. the Parity state's --mode=all) share the cheap 8 GB floor again.
case "$BACKTEST_MODE" in
    all|predictor-backtest|portfolio-optimizer-backtest)
        if [ -z "$INSTANCE_TYPE" ]; then
            echo "  Mode '$BACKTEST_MODE' runs predictor_pipeline → applying ≥8 GB instance floor"
            INSTANCE_TYPES="$_PREDICTOR_RAM_FLOOR_TYPES"
        fi
        ;;
esac

if [ -n "$INSTANCE_TYPE" ]; then
    INSTANCE_TYPES="$INSTANCE_TYPE"  # --instance-type X collapses to single value
fi
echo "  Instance types: $INSTANCE_TYPES"
echo "  Subnets       : $SUBNETS"
echo "  AMI           : $AMI_ID"
echo "  Region        : $AWS_REGION"
echo "  Branch        : $BRANCH"
echo "  Backtest mode : $BACKTEST_MODE"
echo "  Run mode      : $RUN_MODE"
echo "  Preflight-only: $PREFLIGHT_ONLY  (1 = boot + deps + smoke harness + exit 0, NO sweep/sim/parity/evaluator/auto-apply, ZERO writes)"
echo "  Skip phase 4  : $SKIP_PHASE4"
echo "  Skip phases   : ${SKIP_PHASES:-(none)}"
echo "  Only phases   : ${ONLY_PHASES:-(none)}"
echo "  Force all     : $FORCE_ALL"
echo "  Force phases  : ${FORCE_PHASES:-(none)}"
echo "  Dry-run       : $DRY_RUN"
echo "  Skip stages   : ${SKIP_STAGES:-(none)}"
echo "  Freeze eval   : $FREEZE_EVALUATOR"
echo "  Vectorized sw : $USE_VECTORIZED_SWEEP"
echo "  S3 bucket     : $S3_BUCKET"
echo ""

# ── Preflight checks ──────────────────────────────────────────────────────────
if [ ! -f "$REPO_ROOT/config.yaml" ]; then
    echo "ERROR: config.yaml not found — copy from config.yaml.example"
    exit 1
fi

# Locate the executor risk.yaml + predictor predictor.yaml on the dispatcher
# so we can stage them to S3 for the spot. Mirrors the legacy SCP-source
# resolution; only the transport changed.
#
# Experiment-package first (config#1042): risk.yaml resolves from
# alpha-engine-config/experiments/$ALPHA_ENGINE_EXPERIMENT_ID/executor/risk.yaml
# (default experiment `reference`) ahead of the legacy top-level
# alpha-engine-config/executor/risk.yaml, then the repo-local fallback —
# mirroring pipeline_common.load_config + preflight._check_executor_config.
# Behavior-preserving: config#1159 made the package copy byte-identical to legacy.
EXPERIMENT_ID="${ALPHA_ENGINE_EXPERIMENT_ID:-reference}"
EXECUTOR_CONFIG=""
for candidate in \
    "$HOME/alpha-engine-config/experiments/$EXPERIMENT_ID/executor/risk.yaml" \
    "$HOME/Development/alpha-engine-config/experiments/$EXPERIMENT_ID/executor/risk.yaml" \
    "$HOME/alpha-engine-config/executor/risk.yaml" \
    "$HOME/Development/alpha-engine-config/executor/risk.yaml" \
    "$HOME/alpha-engine/config/risk.yaml" \
    "$HOME/Development/alpha-engine/config/risk.yaml"; do
    if [ -f "$candidate" ]; then
        EXECUTOR_CONFIG="$candidate"
        break
    fi
done
if [ -z "$EXECUTOR_CONFIG" ]; then
    echo "ERROR: executor risk.yaml not found in any search path:" >&2
    echo "  ~/alpha-engine-config/experiments/$EXPERIMENT_ID/executor/risk.yaml" >&2
    echo "  ~/Development/alpha-engine-config/experiments/$EXPERIMENT_ID/executor/risk.yaml" >&2
    echo "  ~/alpha-engine-config/executor/risk.yaml" >&2
    echo "  ~/Development/alpha-engine-config/executor/risk.yaml" >&2
    echo "  ~/alpha-engine/config/risk.yaml (legacy)" >&2
    echo "  ~/Development/alpha-engine/config/risk.yaml (legacy)" >&2
    echo "Backtester simulation cannot run without the executor config — silently" >&2
    echo "falling back to risk.yaml.example produces all-placeholder bucket names" >&2
    echo "and ArcticDB KeyNotFoundException deep in the executor-sim run." >&2
    exit 1
fi

PREDICTOR_CONFIG=""
for candidate in \
    "$HOME/alpha-engine-predictor/config/predictor.yaml" \
    "$HOME/Development/alpha-engine-predictor/config/predictor.yaml"; do
    if [ -f "$candidate" ]; then
        PREDICTOR_CONFIG="$candidate"
        break
    fi
done
# PREDICTOR_CONFIG may be empty — predictor backtest is skipped if so.

# ── Dispatcher-side pre-launch preflight (L4485) ─────────────────────────────
# Fail fast on the DISPATCHER, before provisioning a spot, per the standing
# rule "every preflight fails fast before expensive work"
# ([[feedback_preflight_fast_fail_before_expensive_work]]). The existing
# BacktesterPreflight + smoke harness run ON the spot — only AFTER ~10-15 min
# of boot + 3 clones + dep install — so a syntax error or a lib-pin drift
# burns that whole window before surfacing. These checks cost <2 s locally
# and catch the two cheapest-to-miss classes at second zero:
#   (1) py_compile — a SyntaxError anywhere in the load-bearing entrypoints
#       would crash the spot deep in the run. Byte-compile needs no deps.
#   (2) lib-pin drift — requirements.txt's alpha-engine-lib pin must be ≥ the
#       MIN_LIB_VERSION the in-process preflight asserts, or the spot's pip
#       install pulls a version the code rejects (the 2026-04-21 80-min burn).
# Plus a SOFT warning when local tracked .py/.sh edits aren't on origin/$BRANCH
# (the spot clones --branch $BRANCH from GitHub — local-only commits won't run).
pre_launch_preflight() {
    local py
    py="$LIB_PYTHON"
    [ -x "$py" ] || py="$(command -v python3 || echo python3)"

    # (1) Syntax-check the load-bearing entrypoints via ast.parse — a PURE
    #     parse with ZERO filesystem writes. py_compile writes .pyc into
    #     __pycache__, which fails with EACCES on this shared dispatcher
    #     where the cache dir is owned by another uid (root, from a prior
    #     SF run) — a FALSE failure that wrongly blocked a clean launch
    #     (caught live 2026-06-02). ast.parse raises SyntaxError on the same
    #     bug class without touching disk, so it can never false-fail on a
    #     read-only / mixed-ownership tree.
    if ! "$py" -c 'import ast,sys; [ast.parse(open(f).read(), filename=f) for f in sys.argv[1:]]' \
        "$REPO_ROOT/backtest.py" \
        "$REPO_ROOT/evaluate.py" \
        "$REPO_ROOT/preflight.py" \
        "$REPO_ROOT/pipeline_common.py" \
        "$REPO_ROOT/synthetic/predictor_backtest.py" 2>/tmp/prelaunch_syntax.err; then
        echo "ERROR: pre-launch syntax check FAILED — a SyntaxError would crash the spot ~15 min into boot+deps. Fix before launching:" >&2
        cat /tmp/prelaunch_syntax.err >&2
        exit 1
    fi

    # (2) Cross-check the requirements.txt lib pin against preflight.py's floor.
    local pin floor lowest
    pin=$(grep -oE '@v[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/requirements.txt" | head -1 | tr -d '@v')
    floor=$(grep -oE 'MIN_LIB_VERSION[[:space:]]*=[[:space:]]*"[0-9.]+"' "$REPO_ROOT/preflight.py" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    if [ -n "$pin" ] && [ -n "$floor" ]; then
        lowest=$(printf '%s\n%s\n' "$floor" "$pin" | sort -V | head -1)
        if [ "$lowest" != "$floor" ]; then
            echo "ERROR: requirements.txt alpha-engine-lib pin v$pin < preflight.py MIN_LIB_VERSION $floor." >&2
            echo "       The spot's pip install would pull a version the code rejects. Bump the pin or the floor." >&2
            exit 1
        fi
        echo "  pre-launch: lib pin v$pin ≥ MIN_LIB_VERSION $floor ✓"
    else
        echo "  pre-launch: WARNING — could not parse lib pin (pin='$pin' floor='$floor'); skipping pin cross-check" >&2
    fi

    # (3) SOFT: warn on local tracked .py/.sh edits not on origin/$BRANCH.
    local dirty
    dirty=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | grep -E '\.(py|sh)$' || true)
    if [ -n "$dirty" ]; then
        echo "  pre-launch: WARNING — uncommitted tracked .py/.sh changes; the spot clones --branch $BRANCH and will NOT see these:" >&2
        echo "$dirty" | sed 's/^/      /' >&2
    fi
    git -C "$REPO_ROOT" fetch --quiet origin "$BRANCH" 2>/dev/null || true
    local lhead rhead
    lhead=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)
    rhead=$(git -C "$REPO_ROOT" rev-parse "origin/$BRANCH" 2>/dev/null || true)
    if [ -n "$lhead" ] && [ -n "$rhead" ] && ! git -C "$REPO_ROOT" merge-base --is-ancestor "$lhead" "$rhead" 2>/dev/null; then
        echo "  pre-launch: WARNING — local HEAD ($lhead) is not in origin/$BRANCH; the spot clones origin/$BRANCH and will run WITHOUT your local commits. Push first." >&2
    fi

    echo "  pre-launch preflight OK."
}
echo "==> Dispatcher pre-launch preflight (fail-fast before provisioning spot)..."
pre_launch_preflight

# ── Launch spot instance ──────────────────────────────────────────────────────
# Capacity-resilient launch via krepis.ec2_spot (lib v0.26.0+ as
# alpha_engine_lib.ec2_spot / nousergon_lib.ec2_spot; invoked directly via
# krepis per config#1649 — the nousergon_lib re-export shim is guard-less
# under `python -m` on lib >=0.81.0 and silently no-ops).
# Rotates (instance_type × subnet) on InsufficientInstanceCapacity etc.
# Direct fix for the 2026-05-22 incident: THIS LAUNCHER's Evaluator
# invocation failed with InsufficientInstanceCapacity for c5.large in
# us-east-1f.
echo "==> Requesting spot instance (lib CLI rotation: types=[$INSTANCE_TYPES], subnets=[$SUBNETS])..."

INSTANCE_ID=$("$LIB_PYTHON" -m krepis.ec2_spot launch \
    --types "$INSTANCE_TYPES" \
    --subnets "$SUBNETS" \
    --image-id "$AMI_ID" \
    --key-name "$KEY_NAME" \
    --security-group "$SECURITY_GROUP" \
    --iam-profile "$IAM_PROFILE" \
    --name "alpha-engine-backtest-$(date +%Y%m%d)" \
    --region "$AWS_REGION")
ec2_spot_rc=$?
if [ "$ec2_spot_rc" -ne 0 ] || [ -z "$INSTANCE_ID" ]; then
    if [ "$ec2_spot_rc" -eq 64 ]; then
        echo "ERROR: capacity exhausted across all instance_type × subnet combinations" >&2
    fi
    if [ "$ec2_spot_rc" -eq 0 ]; then
      # rc=0 with an EMPTY instance id = the launch layer produced nothing
      # (e.g. the guard-less `-m nousergon_lib.ec2_spot` shim no-op,
      # config#1646 — closed at this launcher's transport by the krepis
      # migration, config#1649). `${ec2_spot_rc:-1}` defaults only when UNSET — a
      # captured 0 passed through and the SF recorded a silent success
      # on 2026-07-03. An empty id must always fail loud.
      echo "ERROR: ec2_spot launch exited 0 without an instance id — failing loud (config#1646)" >&2
      ec2_spot_rc=1
    fi
    exit "$ec2_spot_rc"
fi

echo "  Instance ID: $INSTANCE_ID"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${INSTANCE_ID}"
S3_STAGING_PREFIX="tmp/spot_backtest/${RUN_ID}"
S3_STAGING="s3://${S3_BUCKET}/${S3_STAGING_PREFIX}"

# Last SSM dispatch description — captured for the EXIT-trap diagnostic
# so a non-zero exit prints which run_ssm call ran last. L2246
# (originally LAST_RUN_REMOTE_CMD pre-2026-05-27 SSH→SSM migration).
LAST_SSM_DESC=""

# Cleanup function — always terminate the instance + clean S3 staging,
# with diagnostics on failure.
cleanup() {
    local exit_code=$?
    local _will_relaunch=0 _alert_sev="error" _is_reclaim=0
    echo ""
    echo "==> Dispatcher EXIT (code=$exit_code)"
    if [ "$exit_code" -ne 0 ]; then
        local last_desc="${LAST_SSM_DESC:-<none — failed before any SSM call>}"
        echo "    last run_ssm: $last_desc"
        local state="<not yet provisioned>" state_reason="<none>"
        if [ -n "${INSTANCE_ID:-}" ]; then
            # Capture State.Name + StateTransitionReason BEFORE terminating so
            # the L4485 rc=-1 / empty-output failure class (SSM Failed with no
            # stdout/stderr) is classifiable post-hoc: a spot interruption
            # shows "Server.SpotInstanceTermination" here, a delivery-timeout /
            # genuine crash shows something else. Without this the instance is
            # gone by the time anyone looks and the diagnostics JSON carries
            # empty tails — exactly the 2026-06-01 dead-end. One describe call,
            # flattened to "State.Name<TAB>StateTransitionReason".
            # Capture State.Name + StateReason.Code + StateTransitionReason.
            # The AUTHORITATIVE spot-reclaim signal is StateReason.Code ==
            # Server.SpotInstanceTermination; StateTransitionReason only shows
            # the human "Service initiated (<ts>)" form. Earlier this queried
            # StateTransitionReason ALONE and classified against
            # Server.SpotInstanceTermination — a field/value mismatch that could
            # never match, so two real reclaims on 2026-06-06 hard-failed instead
            # of relaunching. Now both are captured (3 tab-separated fields).
            local _desc reason_code
            _desc=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --query 'Reservations[0].Instances[0].[State.Name,StateReason.Code,StateTransitionReason]' --output text 2>/dev/null || true)
            state=$(printf '%s' "$_desc" | cut -f1)
            reason_code=$(printf '%s' "$_desc" | cut -f2)
            state_reason=$(printf '%s' "$_desc" | cut -f3-)
            [ -z "$state" ] && state="<lookup-failed>"
            [ -z "$reason_code" ] && reason_code="<none>"
            [ -z "$state_reason" ] && state_reason="<none>"
            echo "    spot state: $state"
            echo "    spot state-reason-code: $reason_code"
            echo "    spot state-transition-reason: $state_reason"
        fi
        # L4485-b (classifier fixed 2026-06-06): classify a genuine AWS spot
        # reclaim. Authoritative signal = StateReason.Code ==
        # Server.SpotInstanceTermination. Belt-and-suspenders: also treat a
        # worker already in shutting-down/terminated whose StateTransitionReason
        # is "Service initiated" as a reclaim — AWS tore it down out from under a
        # still-running (exit_code!=0) dispatcher (a real crash/OOM leaves the
        # worker state=running until we terminate it below, so this can't
        # mis-fire on a genuine bug). Every other failure exits as-is so a blind
        # retry never masks a real bug. Downgrade the alert to warning when we
        # will relaunch, so a recovered run does not page as an error.
        case "$reason_code" in
            *Server.SpotInstanceTermination*) _is_reclaim=1 ;;
        esac
        case "$state:$state_reason" in
            shutting-down:*Service\ initiated* | terminated:*Service\ initiated*) _is_reclaim=1 ;;
        esac
        if [ "$_is_reclaim" = "1" ] && [ "${RECLAIM_RELAUNCH_MAX:-0}" -gt 0 ]; then
            _will_relaunch=1
            _alert_sev="warning"
        fi
        # Independent-channel surveillance: fan out via ops_alerts
        # (SNS + flow-doctor forum topics; config#1749 T3). Best-effort:
        # ``|| echo ...`` keeps cleanup running even if Python / lib / SNS /
        # flow-doctor are unreachable — stdout diagnostic above is primary.
        local _alert_python _alert_msg
        _alert_msg="exit_code=$exit_code last_run_ssm='$last_desc' spot_state=$state spot_reason_code='$reason_code' spot_transition_reason='$state_reason' instance_id=${INSTANCE_ID:-<none>} will_relaunch=$_will_relaunch"
        if [ -x "$(dirname "$0")/../.venv/bin/python" ]; then
            _alert_python="$(dirname "$0")/../.venv/bin/python"
        else
            _alert_python="$(command -v python3 || command -v python || echo python)"
        fi
        (cd "$REPO_ROOT" && "$_alert_python" -c "
import sys
from ops_alerts import publish_ops_alert
publish_ops_alert(
    sys.argv[1],
    severity=sys.argv[2],
    source='alpha-engine-backtester/spot_backtest.sh',
)
" "$_alert_msg" "$_alert_sev") \
            > /dev/null 2>&1 || echo "    (ops alert fan-out failed; primary stdout diagnostic above is the surface)"
    fi
    echo "==> Terminating spot instance $INSTANCE_ID..."
    aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --output text > /dev/null 2>&1 || true
    aws s3 rm "$S3_STAGING" --recursive --quiet 2>/dev/null || true
    echo "  Instance terminated; S3 staging cleaned."
    # L4485-b: on a classified spot reclaim, relaunch on a FRESH spot (bounded).
    # The dead worker + its S3 staging are already cleaned above, so re-exec is
    # clean. exec replaces this process, decrementing the budget so the relaunch
    # is bounded; the pending `exit` below is moot. Any non-reclaim failure falls
    # through to the status-preserving exit.
    if [ "$_will_relaunch" = "1" ]; then
        echo "==> Spot RECLAIMED by AWS (reason_code='$reason_code' state='$state' transition='$state_reason') — relaunching on a fresh spot (budget remaining after this: $((RECLAIM_RELAUNCH_MAX - 1)))"
        exec env RECLAIM_RELAUNCH_MAX="$((RECLAIM_RELAUNCH_MAX - 1))" bash "$0" "${_ORIG_ARGS[@]}"
    fi
    # CRITICAL (L4485): re-exit with the captured status. A bash EXIT trap
    # that ends on a successful command (the echo above, or the `|| true`
    # cleanup steps) otherwise leaves the script exiting 0 — which is
    # exactly how a Failed SSM `backtest` step (run_ssm correctly returned
    # 1) was masked as rc=0 to the orchestration wrapper on 2026-06-01,
    # letting a failed run read as success. The cleanup path must never
    # override the primary exit status (inverse of the acceptable EXIT-trap
    # carve-out in [[feedback_no_silent_fails]]).
    exit "$exit_code"
}
trap cleanup EXIT

# Wait for instance to be running
echo "==> Waiting for instance to enter running state..."
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$AWS_REGION"

# ── Stage config files to S3 ─────────────────────────────────────────────────
# Replaces the pre-2026-05-27 SCP path. The spot pulls each file via its
# existing alpha-engine-executor-profile IAM role's s3:GetObject grant.
# .env is no longer staged (#890): its non-secret config moved to config.yaml.
echo "==> Staging configs to ${S3_STAGING}/"

# config.yaml: gitignored backtester runtime config.
aws s3 cp "$REPO_ROOT/config.yaml" "${S3_STAGING}/config.yaml" --region "$AWS_REGION" --quiet
echo "  staged config.yaml"

# Executor risk.yaml: prod path is alpha-engine-config/executor/risk.yaml
# (private config repo pulled daily on ae-dashboard by boot-pull). Legacy
# alpha-engine/config/ path is kept for local dev fallback but has not
# been populated on ae-dashboard since the config-repo split (2026-04-07).
# Hit 2026-04-20: spot silently fell back to risk.yaml.example, executor
# read placeholder signals_bucket="your-research-bucket-name", ArcticDB
# KeyNotFound on a nonexistent bucket. The pre-launch resolver above
# already confirms existence — fail-loud at staging if the file went
# missing in the gap.
aws s3 cp "$EXECUTOR_CONFIG" "${S3_STAGING}/risk.yaml" --region "$AWS_REGION" --quiet
echo "  staged risk.yaml from $EXECUTOR_CONFIG"

# Predictor predictor.yaml: optional — predictor backtest is skipped if
# absent. The pre-launch resolver above sets PREDICTOR_CONFIG="" when
# unfound; encode that state into S3 via a sentinel file so the spot
# bootstrap knows to skip the download.
if [ -n "$PREDICTOR_CONFIG" ]; then
    aws s3 cp "$PREDICTOR_CONFIG" "${S3_STAGING}/predictor.yaml" --region "$AWS_REGION" --quiet
    echo "  staged predictor.yaml from $PREDICTOR_CONFIG"
    STAGED_PREDICTOR_CONFIG=1
else
    echo "  WARNING: predictor.yaml not found — predictor backtest will be skipped"
    STAGED_PREDICTOR_CONFIG=0
fi

# ── Wait for the SSM agent to register ───────────────────────────────────────
# Replaces the old SSH-readiness poll. AL2023 ships the SSM agent; with the
# instance profile's AmazonSSMManagedInstanceCore (in alpha-engine-executor-
# profile) it registers within ~1 min.
echo "==> Waiting for SSM agent to come Online..."
for i in $(seq 1 36); do  # 36 × 5s = 180s budget
    ping=$(aws ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
        --query 'InstanceInformationList[0].PingStatus' \
        --output text --region "$AWS_REGION" 2>/dev/null || true)
    if [ "$ping" = "Online" ]; then
        echo "  SSM agent Online."
        break
    fi
    if [ "$i" -eq 36 ]; then
        echo "ERROR: SSM agent not Online after 180s (instance $INSTANCE_ID)"
        exit 1
    fi
    sleep 5
done

# ── SSM dispatch primitive (lib chokepoint) ──────────────────────────────────
# run_ssm "<description>" [timeout_seconds] <<HEREDOC ... HEREDOC
#
# Thin wrapper around `python -m krepis.ssm_dispatcher run`
# (lib v0.35.0+ as nousergon_lib.ssm_dispatcher; invoked directly via
# krepis per config#1649 — the nousergon_lib re-export shim is guard-less
# under `python -m` on lib >=0.81.0 and silently no-ops). Body read from
# stdin via --script-stdin so the dispatcher's bash parser does not scan
# it for quote/paren balance.
# Records the description in LAST_SSM_DESC so the EXIT trap can name
# which call ran last on failure. Mirrors ae-data PR 2 (#330).
#
# L394 cascade: --diagnostics-bucket + --diagnostics-prefix activate the
# lib v0.39.0 chokepoint that writes a JSON failure record (status +
# command_id + 4KB stdout/stderr tails + instance_id) to
# s3://${S3_BUCKET}/_spot_diagnostics/ae-backtester/{YYYY-MM-DD}.json on
# terminal non-Success. Best-effort write inside the lib — S3 failure
# swallowed; inner SSM exit always preserved. No-op on Success.
run_ssm() {
    local description="$1" timeout_s="${2:-3600}"
    LAST_SSM_DESC="$description"
    "$LIB_PYTHON" -m krepis.ssm_dispatcher run \
        --instance-id "$INSTANCE_ID" \
        --description "backtester: $description" \
        --timeout "$timeout_s" \
        --output-bucket "$S3_BUCKET" \
        --output-key-prefix "${S3_STAGING_PREFIX}/ssm-output" \
        --region "$AWS_REGION" \
        --diagnostics-bucket "$S3_BUCKET" \
        --diagnostics-prefix "_spot_diagnostics/ae-backtester" \
        --script-stdin
}

# ── Bootstrap spot: watchdog + python + git + clone + fetch configs ─────────
# Single SSM call covering: spot-side hard-timeout watchdog,
# python3.12/git install, the 3 HTTPS repo clones, 3 config-file
# fetches from S3 staging (no .env post #890). Watchdog rationale: dispatcher-side
# `trap cleanup EXIT` only fires when THIS bash script exits cleanly.
# If the dispatcher SSM command is cancelled, the dispatcher EC2 is
# stopped mid-run, or the shell gets SIGKILLed, the trap never runs and
# the spot orphans until manually terminated — hit 3 times in April 2026.
# Transient systemd timer fires shutdown -h now after
# MAX_RUNTIME_SECONDS regardless of dispatcher state.
echo "==> Bootstrapping spot (watchdog, python, clone, configs)..."
run_ssm "bootstrap" 600 <<BOOTSTRAP
set -eo pipefail
export HOME=/home/ec2-user XDG_CACHE_HOME=/tmp AWS_REGION=${AWS_REGION} AWS_DEFAULT_REGION=${AWS_REGION}

# Spot-side hard-timeout watchdog (see bootstrap-step rationale above).
systemd-run --on-active=${MAX_RUNTIME_SECONDS} --unit=alpha-engine-watchdog \
    --description='alpha-engine spot hard-timeout' /sbin/shutdown -h now

dnf install -y -q python3.12 python3.12-pip python3.12-devel git gcc 2>/dev/null || \
    dnf install -y -q python3 python3-pip python3-devel git gcc
command -v python3.12 >/dev/null && PYTHON_BIN=python3.12 || PYTHON_BIN=python3
echo "Using: \$(\$PYTHON_BIN --version)"

# flow-doctor is now pulled in via alpha-engine-lib[flow_doctor] from
# requirements.txt — no bundled editable install needed.
# Three HTTPS clones (no SSH key needed; these repos are public siblings).
# Repos were renamed + moved to the nousergon org 2026-06-15
# (alpha-engine-* → crucible-*); local checkout dirs intentionally stay
# alpha-engine-* (dir-name ≠ repo-name split) so every downstream path is
# unchanged. Clone the new slugs explicitly rather than depending on
# GitHub's chained rename/transfer 301 redirect from the old cipher813 paths.
git clone --depth 1 --branch ${BRANCH} https://github.com/nousergon/crucible-backtester.git /home/ec2-user/alpha-engine-backtester
git clone --depth 1 --branch ${BRANCH} https://github.com/nousergon/crucible-executor.git /home/ec2-user/alpha-engine
git clone --depth 1 --branch ${BRANCH} https://github.com/nousergon/crucible-predictor.git /home/ec2-user/alpha-engine-predictor

# Fetch staged configs from S3. (.env no longer staged/fetched — #890.)
aws s3 cp ${S3_STAGING}/config.yaml /home/ec2-user/alpha-engine-backtester/config.yaml --region ${AWS_REGION} --quiet
echo "Fetched config.yaml"

mkdir -p /home/ec2-user/alpha-engine/config
aws s3 cp ${S3_STAGING}/risk.yaml /home/ec2-user/alpha-engine/config/risk.yaml --region ${AWS_REGION} --quiet
echo "Fetched risk.yaml"

if [ "${STAGED_PREDICTOR_CONFIG}" = "1" ]; then
    mkdir -p /home/ec2-user/alpha-engine-predictor/config
    aws s3 cp ${S3_STAGING}/predictor.yaml /home/ec2-user/alpha-engine-predictor/config/predictor.yaml --region ${AWS_REGION} --quiet
    echo "Fetched predictor.yaml"
else
    echo "predictor.yaml NOT staged (predictor backtest will be skipped)"
fi

echo "Bootstrap complete: 3 repos cloned, 3-4 configs fetched from ${S3_STAGING}."
BOOTSTRAP

# ── Install python dependencies ──────────────────────────────────────────────
echo "==> Installing Python dependencies..."
run_ssm "deps" 1200 <<DEPS
set -eo pipefail
export HOME=/home/ec2-user XDG_CACHE_HOME=/tmp AWS_REGION=${AWS_REGION} AWS_DEFAULT_REGION=${AWS_REGION}
cd /home/ec2-user/alpha-engine-backtester

# No .env source (#890): the deps step only needs pip, which resolves the
# alpha-engine-lib git+https URL in requirements.txt without auth (public repo).
# Non-secret runtime config (EMAIL_*, OUTPUT_BUCKET) is read from config.yaml
# by the python pipeline and by the per-stage BUCKET resolution below.
command -v python3.12 >/dev/null && PIP="python3.12 -m pip" || PIP="python3 -m pip"

\$PIP install --upgrade pip -q
\$PIP install -q -r requirements.txt

# Also install predictor deps (needed for GBM inference + feature computation).
# The predictor + backtester alpha-engine-lib pins are now ALIGNED at v0.53.0
# (fleet lockstep — predictor #238 / L4513), so this co-install no longer
# downgrades the lib. Background: when the predictor lagged at v0.47.0, installing
# it here SECOND silently downgraded the lib below quant.stats (first shipped
# v0.49.0), breaking evaluate.py's import every run (the 2>/dev/null hid pip's
# downgrade note). Alignment fixes the instance; the GUARD below fixes the CLASS.
cd /home/ec2-user/alpha-engine-predictor
if [ -f requirements.txt ]; then
    \$PIP install -q -r requirements.txt 2>/dev/null || true
fi

# Fail-loud dependency GUARD (L4513 class fix). Assert the nousergon-lib
# modules the Evaluator imports are actually present AFTER all installs — so if a
# future sibling-repo pin drift ever downgrades the lib below quant.stats, this
# breaks LOUD at deps time instead of silently at evaluate.py's import weeks
# later. Per feedback_no_silent_fails. PYBIN derives from PIP ("py -m pip" -> py).
# The lib was renamed alpha-engine-lib -> nousergon-lib (alpha_engine_lib is now a
# deprecated import alias); requirements.txt installs the nousergon-lib
# distribution, so the guard MUST verify via the real module + distribution name
# -- "pip show alpha-engine-lib" returns nothing and exits 1 under pipefail.
# (NB: no backticks in this heredoc comment -- they would command-substitute.)
cd /home/ec2-user/alpha-engine-backtester
PYBIN="\${PIP% -m pip}"
\$PYBIN -c "import nousergon_lib.quant.stats.multiple_testing, nousergon_lib.quant" || {
    echo "FATAL: nousergon-lib is missing quant.stats — a co-installed sibling repo's pin likely downgraded it below v0.49.0. Resolved version:" >&2
    \$PIP show nousergon-lib | grep -E '^Version:' >&2 || true
    exit 1
}
\$PIP show nousergon-lib | grep -E '^Version:'

# Force numpy<2 after all deps (pyarrow compiled against numpy 1.x)
\$PIP install -q 'numpy<2'

echo "Dependencies installed."
DEPS

# ── Predictor sector_map cache fetch ─────────────────────────────────────────
# Only sector_map.json is consumed (predictor_backtest.load_sector_map).
# The former price_cache_slim sync was Wave-4 dead staging —
# predictor_backtest loads prices+features from ArcticDB
# (load_universe_from_arctic), never the local cache parquets; verified
# no data/cache/*.parquet reader exists. Removed in Wave-4 PR4.
echo "==> Downloading predictor sector_map from S3..."
run_ssm "predictor-cache" 300 <<'CACHE'
set -eo pipefail
export HOME=/home/ec2-user XDG_CACHE_HOME=/tmp
CACHE_DIR="/home/ec2-user/alpha-engine-predictor/data/cache"
mkdir -p "$CACHE_DIR"
# Wave-3 reader migration (ROADMAP L1401): try new
# reference/price_cache/sector_map.json first, fall back to legacy
# predictor/price_cache/ during the write-both soak. Wave-4: former
# `aws s3 sync price_cache_slim/` removed — dead staging
# (predictor_backtest loads from ArcticDB, never reads
# data/cache/*.parquet).
aws s3 cp s3://alpha-engine-research/reference/price_cache/sector_map.json "$CACHE_DIR/sector_map.json" 2>/dev/null \
    || aws s3 cp s3://alpha-engine-research/predictor/price_cache/sector_map.json "$CACHE_DIR/sector_map.json" 2>/dev/null \
    || true
echo "Predictor cache dir: sector_map.json $([ -f "$CACHE_DIR/sector_map.json" ] && echo present || echo MISSING)"
CACHE

# ── Build env export command ─────────────────────────────────────────────────
# PYTHONUNBUFFERED=1: line-buffering stdout/stderr so SSM ships log lines as
# they're emitted. Without this, stdout is block-buffered when the agent
# captures it to CloudWatch — the 2026-04-22 4th Saturday SF dry-run lost
# ~16 minutes of in-flight output when the SSM agent died mid-run and
# buffered lines never reached the log. Combined with the phase markers
# in pipeline_common.phase (which explicit-flush after each START/END),
# this closes the "silent 110-minute phase" blind spot. Paired with
# `python -u` on each backtest.py invocation below as belt-and-suspenders.
#
# ALPHA_ENGINE_DECISION_CAPTURE_SUPPRESS=true is exported in ENV_SOURCE so it
# overrides whatever the dispatcher's env passes through (#890 removed the .env
# source that previously preceded it; the suppress export is unconditional now).
# The executor's decision_capture short-circuits at is_decision_capture_enabled()
# when this flag is truthy. Sim hot loop (param_sweep × N_dates × N_positions)
# would otherwise emit ~50k-200k per-decision S3 PUTs and blow the
# simulation_pipeline 2700s watchdog (observed 2026-05-13 spot run
# adhoc-skipto-backtester-20260513-2333). Capture artifacts exist for
# production observability — they have no semantic meaning in the sweep.
# Paired with alpha-engine #177.
# AWS_REGION/AWS_DEFAULT_REGION: #890 removed the sourced .env entirely, so
# the region env vars boto3 + lib preflight require must be exported here.
# Same #247 regression class as alpha-engine-data's spot scripts; this script
# was in a sibling repo the original arc didn't touch. System is single-region
# us-east-1 (matches this file's own ${AWS_REGION:-us-east-1} defaults).
# Origin: 2026-05-16 Saturday SF PredictorTraining failure (spot_train.sh
# sibling) — audited forward to prevent the identical Backtester/Parity/
# Evaluator failure. No .env is sourced; OUTPUT_BUCKET is now read from the
# staged config.yaml at each per-stage BUCKET resolution below.
ENV_SOURCE='export XDG_CACHE_HOME=/tmp; export PYTHONUNBUFFERED=1; export ALPHA_ENGINE_DECISION_CAPTURE_SUPPRESS=true; export AWS_REGION=us-east-1; export AWS_DEFAULT_REGION=us-east-1 ALPHA_ENGINE_DEPLOYED=1; command -v python3.12 >/dev/null && PYTHON_BIN=python3.12 || PYTHON_BIN=python3; export PYTHON_BIN;'

# Spot-side python is resolved inline per SSM step via PYTHON_BIN in the
# ENV_SOURCE above. The pre-2026-05-27 SSH transport captured this on the
# dispatcher with `REMOTE_PYTHON=$(run_remote "command -v ...")` — under
# SSM there's no native way to capture inner-script stdout into a
# dispatcher variable, so the resolution is repeated inside each heredoc
# (cheap — just a $PATH probe). REMOTE_PYTHON kept as a name alias for
# the per-heredoc reference, set to the env-var form $PYTHON_BIN that
# ENV_SOURCE binds at runtime on the spot.
REMOTE_PYTHON='$PYTHON_BIN'

# ── Preflight-only (Friday shell_run dry path) ──────────────────────────────
# ROADMAP "Friday shell-run — per-module dry-path activation" owed-item #3.
# Placed AFTER the real boot/clone/deps/config-upload (so the bootstrap
# path — lib-pin resolution, sys.path, predictor cache sync, image deps —
# is genuinely exercised) and STRICTLY BEFORE both the --smoke-only block
# (per-phase smoke modes + the evaluate.py S3-probe diagnostics) and the
# full-backtest heredoc.
#
# Runs ONLY `backtest.py --mode=smoke` — the EXISTING bootstrap-class
# smoke harness from PRs #43-#48 (BacktesterPreflight: lib-version /
# imports / predictor-weights presence / executor-config validation, then
# _runtime_smoke: universe-symbols + per-ticker ArcticDB read + recent
# signals.json load + Layer-1A GBM load/predict — all S3 *reads*, ~30-60s).
# We REUSE backtest.py's existing --mode=smoke (no new harness): per
# backtest.py:4180-4184 it runs preflight + _runtime_smoke then `return`s
# BEFORE _init_pipeline / the simulation / the optimizer, so it itself
# performs zero config writes and makes no external API (yfinance/
# Anthropic) data fetch.
#
# Hard invariant proof (what is statically unreachable under this flag):
#   * The per-phase smoke loop (smoke-simulate / smoke-param-sweep /
#     smoke-predictor-backtest / smoke-phase4 / smoke-predictor-param-sweep)
#     and the `evaluate.py --mode diagnostics` S3-probe block live INSIDE
#     the `if [ "$RUN_MODE" = "smoke-only" ]` body below — the `exit 0`
#     here never reaches it.
#   * The full-backtest heredoc (backtest stage / pit_parity / parity /
#     evaluator) and its config/{executor,scoring,predictor,research,
#     scanner}_params*.json optimizer auto-apply (evaluate.py --upload,
#     non-frozen) live further below — also unreachable.
#   * No CloudWatch heartbeat, no parity_report.json / parity_metrics.csv
#     upload, no reporter S3 upload — all of those are past this exit.
# Net: smoke harness (read-only) runs, then exit 0. Zero external API
# calls, zero S3/config writes. The `trap cleanup EXIT` still fires and
# terminates the spot instance.
if [ "$PREFLIGHT_ONLY" = "1" ]; then
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  PREFLIGHT-ONLY (Friday shell_run dry path)"
    echo "  boot + deps done; running bootstrap-class smoke harness only,"
    echo "  then exit 0 — NO sweep / sim / parity / evaluator / auto-apply,"
    echo "  ZERO external API calls, ZERO S3/config writes."
    echo "═══════════════════════════════════════════════════════════════"
    run_ssm "preflight-only" 900 <<PREFLIGHT
set -eo pipefail
cd /home/ec2-user/alpha-engine-backtester
${ENV_SOURCE}

# backtest.py --mode=smoke = BacktesterPreflight + _runtime_smoke, then
# returns 0 BEFORE _init_pipeline / simulation / optimizer (see
# backtest.py:4180). No --upload, no full mode, no config write.
echo "==> Preflight: backtest.py --mode=smoke"
$REMOTE_PYTHON -u backtest.py --mode=smoke --log-level INFO 2>&1
PREFLIGHT

    echo ""
    echo "==> Preflight-only PASSED — bootstrap-class smoke clean."
    echo "==> Instance will be terminated (no sweep/sim/parity/evaluator,"
    echo "    no config/*.json auto-apply, no S3/config writes performed)."
    exit 0
fi

# ── Smoke test ────────────────────────────────────────────────────────────────
if [ "$RUN_MODE" = "smoke-only" ]; then
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  SMOKE TEST"
    echo "═══════════════════════════════════════════════════════════════"

    # backtest.py --mode=smoke runs BacktesterPreflight + runtime smoke
    # (end-to-end with minimal data: universe symbols, per-ticker Arctic
    # read, recent signals.json load, Layer-1A GBM load + predict) and
    # exits 0. Keeps the smoke path in lockstep with what full modes do
    # at startup — no drift between bash-driven smoke and in-process
    # pipeline validation. Evaluate-mode smoke follows: artifact-read
    # path + BacktesterPreflight(mode="evaluate").
    run_ssm "smoke" 3600 <<SMOKE
set -eo pipefail
cd /home/ec2-user/alpha-engine-backtester
${ENV_SOURCE}

# OUTPUT_BUCKET formerly came from the sourced .env (#890 removed it). Read it
# from the staged config.yaml instead, with the same default fallback so
# \`\`set -u\`\` never trips on a missing key. config.yaml was fetched into cwd
# by BOOTSTRAP; \$PYTHON_BIN is set by ENV_SOURCE.
BUCKET="\$(\$PYTHON_BIN -c 'import yaml,sys; print((yaml.safe_load(open(\"config.yaml\")) or {}).get(\"output_bucket\") or \"alpha-engine-research\")' 2>/dev/null || echo alpha-engine-research)"

# Per-mode smoke summary — collected throughout the run and printed as
# a single table at the end. Each entry: "name|status|duration|budget|usage".
# Populated regardless of pass/fail so partial runs still show which
# modes completed before the failure.
declare -a _SMOKE_SUMMARY=()

_smoke_record() {
    # args: name, status ("ok" | "FAIL"), duration_s, budget_s (may be ""), usage_pct (may be "")
    _SMOKE_SUMMARY+=("\$1|\$2|\$3|\$4|\$5")
}

_smoke_extract_budget() {
    # Pull "N.Ns <= N.Ns (N% of budget)" from a log file's last
    # budget-check line. Emits "budget_s<TAB>usage_pct" or empty.
    local log_file="\$1"
    local line
    line="\$(grep -oE 'budget check: [0-9.]+s <= [0-9.]+s \([0-9]+% of budget\)' "\$log_file" | tail -1 || true)"
    [ -z "\$line" ] && return
    local budget usage
    budget="\$(echo "\$line" | grep -oE '<= [0-9.]+s' | grep -oE '[0-9.]+s')"
    usage="\$(echo "\$line" | grep -oE '\([0-9]+%' | tr -d '(%')%"
    printf '%s\t%s' "\$budget" "\$usage"
}

_smoke_run_mode() {
    # Run one backtest.py --mode=X, tee output, record to summary.
    # Returns non-zero on Python failure so caller can decide to break.
    local mode="\$1"
    local log_file="/tmp/smoke_\${mode//\//_}.log"
    local start=\$SECONDS
    local status="ok"

    echo ""
    echo "==> Smoke: backtest.py --mode=\$mode $SMOKE_PHASE_FLAGS"
    if ! $REMOTE_PYTHON -u backtest.py --mode=\$mode --log-level INFO $SMOKE_PHASE_FLAGS 2>&1 | tee "\$log_file"; then
        status="FAIL"
    fi
    local dur=\$((SECONDS - start))

    local budget="" usage=""
    local extracted
    extracted="\$(_smoke_extract_budget "\$log_file")"
    if [ -n "\$extracted" ]; then
        budget="\${extracted%%\$'\t'*}"
        usage="\${extracted##*\$'\t'}"
    fi

    _smoke_record "\$mode" "\$status" "\${dur}s" "\$budget" "\$usage"
    [ "\$status" = "ok" ]
}

_smoke_print_summary() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  SMOKE SUMMARY"
    echo "═══════════════════════════════════════════════════════════════"
    printf "  %-28s %-8s %-10s %-10s %-8s\n" "Mode" "Status" "Duration" "Budget" "Usage"
    printf "  %s\n" "─────────────────────────────────────────────────────────────────────"
    local any_fail=0
    for entry in "\${_SMOKE_SUMMARY[@]}"; do
        IFS='|' read -r name status dur budget usage <<< "\$entry"
        printf "  %-28s %-8s %-10s %-10s %-8s\n" "\$name" "\$status" "\$dur" "\${budget:-–}" "\${usage:-–}"
        [ "\$status" = "FAIL" ] && any_fail=1
    done
    echo "═══════════════════════════════════════════════════════════════"
    if [ "\$any_fail" = "1" ]; then
        echo "  RESULT: FAIL (one or more modes did not pass)"
    else
        echo "  RESULT: PASS (all \${#_SMOKE_SUMMARY[@]} modes ok)"
    fi
    echo "═══════════════════════════════════════════════════════════════"
}

# Always print summary, even if a mode aborts mid-run.
trap '_smoke_print_summary' EXIT

# backtest.py --mode=smoke: preflight + runtime smoke (universe symbols,
# per-ticker Arctic read, recent signals.json load, Layer-1A GBM load +
# predict). Keeps smoke in lockstep with what full modes do at startup.
if ! _smoke_run_mode smoke; then
    echo "ERROR: smoke preflight FAILED — aborting"
    exit 1
fi

# Per-phase smoke harness — exercise each pipeline phase-family with a
# tiny fixture (few dates, tiny param grid, short GBM lookback) and
# enforce per-mode wall-clock budgets from timing_budget.yaml. Ordered
# fastest → slowest so a failure in an earlier mode short-circuits
# the harder ones. ROADMAP Backtester P0 #3.
#
# Timestamp capture for the L280 SUPPRESS contract canary below — the
# smoke-param-sweep mode is the hot-loop site that would emit ~50k-200k
# decision_artifacts/ S3 PUTs if SUPPRESS were broken. Captured BEFORE
# the loop so the canary's LastModified filter covers every smoke mode
# that touches executor code, not just smoke-param-sweep.
SMOKE_SWEEP_START_ISO=\$(date -u +%Y-%m-%dT%H:%M:%S)
for SMOKE_PHASE_MODE in smoke-simulate smoke-param-sweep smoke-predictor-backtest smoke-phase4 smoke-predictor-param-sweep; do
    if ! _smoke_run_mode "\$SMOKE_PHASE_MODE"; then
        echo "ERROR: smoke phase \$SMOKE_PHASE_MODE FAILED — aborting smoke-only run"
        exit 1
    fi
done

# ── L280 SUPPRESS contract canary ─────────────────────────────────────────
# Asserts ALPHA_ENGINE_DECISION_CAPTURE_SUPPRESS=true (exported in
# ENV_SOURCE above) actually prevented decision_artifacts/ S3 PUTs
# during the smoke window. Catches operational-side regressions the
# in-process CI test (tests/test_param_sweep_decision_capture_suppress.py)
# cannot: ENV_SOURCE drift on the spot AMI / .env override resetting
# the flag / IAM-role substitution that lib gating doesn't see.
# Composes with the CI test which catches code-review-time regressions
# (env-var rename, gate-semantics flip, new bypassing capture site).
echo ""
echo "==> [suppress-canary] checking decision_artifacts/ writes during smoke window..."
SUPPRESS_PREFIX="decision_artifacts/\$(date -u +%Y/%m/%d)/"
SUPPRESS_HITS=\$(aws s3api list-objects-v2 \\
    --bucket "\${BUCKET}" \\
    --prefix "\${SUPPRESS_PREFIX}" \\
    --query "Contents[?LastModified >= '\${SMOKE_SWEEP_START_ISO}'] | length(@)" \\
    --output text 2>/dev/null || echo 0)
SUPPRESS_HITS=\${SUPPRESS_HITS:-0}
[ "\${SUPPRESS_HITS}" = "None" ] && SUPPRESS_HITS=0
if [ "\${SUPPRESS_HITS}" != "0" ]; then
    echo "ERROR [suppress-canary] \${SUPPRESS_HITS} \${SUPPRESS_PREFIX} keys appeared since \${SMOKE_SWEEP_START_ISO} — ALPHA_ENGINE_DECISION_CAPTURE_SUPPRESS contract REGRESSED. Investigate ENV_SOURCE export / .env override / lib gating semantics / new bypassing capture site. ROADMAP L280." >&2
    _smoke_record "suppress-canary" "FAIL" "0s" "" ""
    exit 1
fi
echo "[suppress-canary] OK — zero \${SUPPRESS_PREFIX} writes since \${SMOKE_SWEEP_START_ISO} (SUPPRESS contract holding)"
_smoke_record "suppress-canary" "ok" "0s" "" ""

echo ""
echo "==> Resolving most recent backtest artifact date from s3://\${BUCKET}/backtest/..."
# Pick the most-recent date that ALSO has portfolio_stats.json on S3.
# The plain "sort | tail -1" approach picked stale empty prefixes
# created by prior half-complete runs (observed 2026-04-24 smoke: a
# 2026-04-24/ prefix existed but had no artifacts, causing evaluate.py
# to hard-fail with "All critical simulation artifacts missing").
# Excluding hidden prefixes (.smoke/, .dry-run/) keeps the probe
# pointing at production dates.
LATEST_DATE=""
while IFS= read -r candidate; do
    [ -z "\$candidate" ] && continue
    case "\$candidate" in .*) continue ;; esac
    if aws s3api head-object --bucket "\${BUCKET}" --key "backtest/\$candidate/portfolio_stats.json" >/dev/null 2>&1; then
        LATEST_DATE="\$candidate"
        break
    fi
done < <(aws s3 ls "s3://\${BUCKET}/backtest/" | awk '/PRE / {print \$2}' | tr -d '/' | sort -r)
if [ -z "\$LATEST_DATE" ]; then
    echo "ERROR: no backtest/{date}/ prefix with portfolio_stats.json found in s3://\${BUCKET}/backtest/"
    _smoke_record "evaluate-diagnostics" "FAIL" "0s" "" ""
    exit 1
fi
echo "Using backtest date: \$LATEST_DATE"

echo ""
echo "==> Smoke: evaluate.py --mode diagnostics --freeze --date \$LATEST_DATE"
_EVAL_START=\$SECONDS
_EVAL_STATUS="ok"
if ! $REMOTE_PYTHON -u evaluate.py --mode diagnostics --freeze --date "\$LATEST_DATE" --log-level INFO 2>&1 | tail -30; then
    _EVAL_STATUS="FAIL"
fi
_EVAL_DUR=\$((SECONDS - _EVAL_START))
_smoke_record "evaluate-diagnostics" "\$_EVAL_STATUS" "\${_EVAL_DUR}s" "" ""

echo ""
echo "Smoke test complete."
# Summary prints via trap on exit
SMOKE

    echo "==> Smoke-only mode — instance will be terminated."
    exit 0
fi

# ── Full backtest ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  FULL BACKTEST (--mode $BACKTEST_MODE)"
echo "═══════════════════════════════════════════════════════════════"
echo ""

run_ssm "backtest" "$MAX_RUNTIME_SECONDS" <<BACKTEST
set -eo pipefail
cd /home/ec2-user/alpha-engine-backtester
${ENV_SOURCE}

# BUCKET used across all three stages. OUTPUT_BUCKET formerly came from the
# sourced .env (#890 removed it); read it from the staged config.yaml instead,
# falling back to the default so \`\`set -u\`\` doesn't blow up on a missing key.
# Matches the smoke-only heredoc's line. config.yaml is in cwd (fetched by
# BOOTSTRAP); \$PYTHON_BIN is set by ENV_SOURCE.
BUCKET="\$(\$PYTHON_BIN -c 'import yaml,sys; print((yaml.safe_load(open(\"config.yaml\")) or {}).get(\"output_bucket\") or \"alpha-engine-research\")' 2>/dev/null || echo alpha-engine-research)"
# SKIP_STAGES baked in from the dispatcher's --skip-stages flag. Stages in
# this CSV are skipped with a loud ⊘ echo; everything else runs.
SKIP_STAGES="${SKIP_STAGES}"
# PIT_PARITY_ENABLED baked in from the dispatcher (default 1 / ON since
# 2026-05-17). Same mechanism as SKIP_STAGES — the dispatcher-side value is
# interpolated at heredoc-generation time so the runtime gate below resolves
# it on the spot instance.
PIT_PARITY_ENABLED="${PIT_PARITY_ENABLED}"
# RUN_DATE baked in from the dispatcher (resolved once from the injected
# env / --run-date / wall-clock fallback above) so backtest + param-sweep
# + parity + pit_parity + evaluator uploads all land under the SAME
# backtest/{date}/ prefix — even across the 3 separate spot stages of one
# Saturday SF run that may straddle UTC midnight. Same gen-time
# interpolation as SKIP_STAGES / PIT_PARITY_ENABLED above; the prior
# per-spot \$(date -u) recompute was the 2026-05-17 Evaluator date-split.
RUN_DATE="${RUN_DATE}"

_stage_skipped() {
    case ",\${SKIP_STAGES}," in
        *",\$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

# ── Stage: backtest ─────────────────────────────────────────────────────────
# If backtest.py fails we exit non-zero so parity + evaluator never run
# against stale or missing artifacts — the evaluator would otherwise
# auto-promote garbage params to S3. Fail loud so the spot run is marked
# failed, the heartbeat metric is not emitted, and the Step Function catches
# it. Replaces the previous || { echo WARNING } swallow that silently let
# evaluator run against invalid sweep results and was the root cause of
# multiple undetected param oscillations.
if _stage_skipped backtest; then
    echo "⊘ stage=backtest SKIPPED (--skip-stages=\${SKIP_STAGES})"
else
    echo "▶ stage=backtest START at \$(date -u +%H:%M:%S)"
    # --date "\${RUN_DATE}" pins backtest.py's artifact prefix to the
    # SF-stamped run date. Without it backtest.py defaults --date to its
    # own date.today() on this spot instance, so the backtest stage wrote
    # backtest/2026-05-17/ while the later Evaluator spot looked under
    # backtest/2026-05-18/ — the 2026-05-17 date-split. Parity / pit_parity
    # / evaluator already thread \${RUN_DATE}; this closes the last gap so
    # ALL stages key off the single SF-declared date.
    if ! $REMOTE_PYTHON -u backtest.py --mode $BACKTEST_MODE --date "\${RUN_DATE}" --upload --log-level INFO $BACKTEST_SKIP_PHASE4_FLAG $BACKTEST_PHASE_FLAGS 2>&1; then
        echo "ERROR: backtest.py failed. Spot run marked FAILED — check" >&2
        echo "       flow-doctor alerts. Parity + evaluator stages skipped" >&2
        echo "       to prevent auto-promotion of unvalidated configs." >&2
        exit 1
    fi
    echo "▶ stage=backtest END at \$(date -u +%H:%M:%S)"
fi

# ── Stage: pit_parity (observational, DEFAULT ON, NON-BLOCKING) ─────────────
# Proof-of-impact for point-in-time discipline (ROADMAP L2371 / plan §D4):
# runs the predictor backtest both ways (legacy single-pass vs
# --walk-forward) and emits the skilled-risk-basket contamination report to
# s3://{bucket}/backtest/{RUN_DATE}/pit_parity.json. This is the input to
# the manual, Brian-gated --walk-forward default flip (plan §5).
#
# DEFAULT ON 2026-05-17 (Brian: "switch pit to on"). Runs an extra
# predictor-sim pass (~+1 predictor backtest; bounded, the predictor
# pipeline is ~4 min / ~8% of the 1800s cap). NEVER fails the spot run
# (|| true) — observational only, writes no configs, and does NOT change
# --walk-forward (the optimizer-feeding default stays OFF; flipping it is
# the separate post-review L2371 step). Opt out per run: --no-pit-parity
# or PIT_PARITY_ENABLED=0. Runtime fallback stays :-0 (belt-and-suspenders
# if the dispatcher bake is ever bypassed).
# L4486 (2026-06-05): gate is on the dedicated pit_parity stage token, NOT
# the backtest token, so pit_parity runs in the standalone Parity SF state
# (which passes --skip-stages=backtest,evaluator: backtest skipped but
# pit_parity NOT skipped) in a FRESH process with full RAM headroom -- instead
# of stacked inside PredictorBacktest after the main predictor pipeline already
# held ~3.5 GB. The SF turns it OFF in PredictorBacktest via --no-pit-parity so
# it still fires EXACTLY ONCE.
# NOTE: this comment lives inside the unquoted heredoc, so it must contain NO
# backticks or dollar-paren -- they would be command-substituted at heredoc
# construction (the 2026-06-05 "pit_parity: command not found" noise).
if [ "\${PIT_PARITY_ENABLED:-0}" = "1" ] && ! _stage_skipped pit_parity; then
    echo "▶ stage=pit_parity START at \$(date -u +%H:%M:%S) (observational, non-blocking)"
    # Swallow on non-zero exit per feedback_no_silent_fails secondary-
    # observability carve-out: (a) failure mode swallowed = pit_parity
    # exception path (backtester continues either way); (b) primary
    # deliverable survives = weights archive + sweep + evaluator pipeline
    # are independent of pit_parity; (c) concrete recording surfaces =
    # (1) S3 artifact at backtest/{date}/pit_parity.json with status=failed
    # always emitted by backtest.py::main pit_parity branch (since
    # 2026-05-27); (2) Telegram + SNS alert via alpha_engine_lib.alerts
    # (sev=warning, dedup-keyed on run_date). 2026-05-17→2026-05-24
    # incident: this swallow ate 4 RecursionError silently before the
    # contract was added.
    $REMOTE_PYTHON -u backtest.py --mode predictor-backtest --pit-parity \\
        --date "\${RUN_DATE}" --log-level INFO 2>&1 \\
        || echo "WARNING: pit_parity stage failed (observational — spot run continues; failure-artifact + Telegram alert published by the inner Python)"
    echo "▶ stage=pit_parity END at \$(date -u +%H:%M:%S)"
else
    echo "⊘ stage=pit_parity SKIPPED (PIT_PARITY_ENABLED!=1 or --skip-stages contains pit_parity — runs ONCE in the standalone Parity state per L4486)"
fi

# ── Stage: parity ───────────────────────────────────────────────────────────
# Parity is OBSERVABILITY, not a gate. Each Saturday SF run produces:
#   * parity_report.json — per-run drill-down (count + ticker-set + field
#     divergence breakdowns), uploaded to s3://{bucket}/backtest/{date}/
#   * parity_metrics.csv — append one row per run with capture_rate,
#     ticker_jaccard_avg, count_divergence_rms, field_diff_rate,
#     n_lifecycle_skipped. Time series at
#     s3://{bucket}/backtest/parity_metrics.csv. The metric trend is the
#     load-bearing signal; step-changes trigger investigation.
# The pytest assertion was removed (test always passes — its job is to
# generate the artifacts). The spot run does NOT fail the SF on parity
# divergence: 0% historical parity is structurally unreachable for a
# system with weekly auto-tuned configs and evolving executor code.
# See tests/test_parity_replay.py module docstring for the full rationale.
# Setup-level failures (missing trades.db, ArcticDB unreachable) are still
# fatal here — those are real infrastructure breakage, not "expected drift".
if _stage_skipped parity; then
    echo "⊘ stage=parity SKIPPED (--skip-stages=\${SKIP_STAGES})"
else
    echo "▶ stage=parity START at \$(date -u +%H:%M:%S)"
    PARITY_TRADES_DB="/tmp/trades_latest.db"
    PARITY_REPORT_DIR="/tmp/parity_report"
    mkdir -p "\$PARITY_REPORT_DIR"

    if ! aws s3 cp "s3://\${BUCKET}/trades/trades_latest.db" "\$PARITY_TRADES_DB" --quiet; then
        echo "ERROR: could not download trades_latest.db from S3 — parity cannot run" >&2
        echo "       This is infrastructure breakage (not divergence) — failing spot." >&2
        exit 1
    fi

    PARITY_EXIT=0
    # USE_REAL_ARCTICDB=1 tells tests/conftest.py to skip the default
    # MagicMock stub so the integration test hits real ArcticDB.
    # PARITY_RUN_DATE pins the time-series CSV's run_date column to
    # today's RUN_DATE so re-runs of a single Saturday cohort overwrite
    # idempotently rather than producing duplicate rows.
    TRADES_DB_PATH="\$PARITY_TRADES_DB" \\
    SIGNALS_BUCKET="\${BUCKET}" \\
    PARITY_REPORT_DIR="\$PARITY_REPORT_DIR" \\
    PARITY_RUN_DATE="\${RUN_DATE}" \\
    USE_REAL_ARCTICDB=1 \\
    $REMOTE_PYTHON -m pytest tests/test_parity_replay.py -m parity -v 2>&1 || PARITY_EXIT=\$?

    # Upload the per-run report. The time-series CSV is appended by the
    # test itself (see append_parity_metrics_row) — best-effort, errors
    # WARN-not-FAIL since the per-run report is the authoritative artifact.
    if [ -f "\$PARITY_REPORT_DIR/parity_report.json" ]; then
        aws s3 cp "\$PARITY_REPORT_DIR/parity_report.json" \\
            "s3://\${BUCKET}/backtest/\${RUN_DATE}/parity_report.json" --quiet \\
            && echo "Uploaded parity_report.json to s3://\${BUCKET}/backtest/\${RUN_DATE}/" \\
            || echo "WARNING: failed to upload parity_report.json (non-fatal)"
    fi

    # Pytest exit codes:
    #   0 = test ran (always-pass, since divergence is observability not gate)
    #   non-zero with parity_report.json present = setup-level error inside the
    #     test body (e.g. ArcticDB read failure on integration path); flag a
    #     WARNING but don't fail the spot — operator can still inspect the
    #     report. The SF alarm should fire on real infrastructure breakage
    #     (the s3 cp failure above), not on observability test signaling.
    if [ "\$PARITY_EXIT" != "0" ]; then
        echo "WARNING: parity pytest exited \$PARITY_EXIT (likely setup-side error)." >&2
        echo "         See s3://\${BUCKET}/backtest/\${RUN_DATE}/parity_report.json (if present)." >&2
        echo "         Continuing spot run — parity is observability, not a gate." >&2
    fi
    echo "▶ stage=parity END at \$(date -u +%H:%M:%S)"
fi

# ── Stage: evaluator ────────────────────────────────────────────────────────
# Runs evaluate.py against today's backtest artifacts in S3. Consolidated
# into the spot step 2026-04-24 — the SF's dedicated Evaluator states
# (CheckSkipEvaluator, CheckEvaluatorFreeze, Evaluator, EvaluatorFrozen,
# WaitForEvaluator, CheckEvaluatorStatus, EvaluatorWait, ExtractEvaluatorError)
# were retired. --freeze-evaluator controls config-promotion (freeze =
# diagnostic-only, no config writes). Default is live-apply for the Sat SF;
# manual iteration runs should pass --freeze-evaluator.
if _stage_skipped evaluator; then
    echo "⊘ stage=evaluator SKIPPED (--skip-stages=\${SKIP_STAGES})"
else
    echo "▶ stage=evaluator START at \$(date -u +%H:%M:%S) freeze=${FREEZE_EVALUATOR}"
    _EVAL_FREEZE=""
    if [ "${FREEZE_EVALUATOR}" = "true" ]; then
        _EVAL_FREEZE="--freeze"
    fi
    if ! $REMOTE_PYTHON -u evaluate.py --mode all --upload \$_EVAL_FREEZE --log-level INFO 2>&1; then
        echo "ERROR: evaluate.py failed. Spot run marked FAILED." >&2
        exit 1
    fi
    echo "▶ stage=evaluator END at \$(date -u +%H:%M:%S)"
fi

echo ""
echo "All requested stages complete at \$(date)"
BACKTEST

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Backtest complete. Instance will be terminated."
echo "═══════════════════════════════════════════════════════════════"

# Per-stage CloudWatch heartbeats. Each stage gets its own heartbeat so
# the Saturday SF can split backtest+parity (Backtester state) from
# evaluator (Evaluator state) across two SF states without conflating
# their alarms. Backtester heartbeat fires only when both backtest and
# parity ran (parity is observability for backtest output — they form
# one semantic unit). Evaluator heartbeat fires only when evaluator ran.
# Stages listed in --skip-stages are excluded from heartbeat emission.
_emit_heartbeat() {
    local _process="$1"
    aws cloudwatch put-metric-data \
        --namespace "AlphaEngine" \
        --metric-name "Heartbeat" \
        --dimensions "Process=${_process}" \
        --value 1 --unit "Count" \
        --region "${AWS_REGION:-us-east-1}" 2>/dev/null \
        && echo "Heartbeat emitted: ${_process}" \
        || echo "WARNING: Failed to emit heartbeat for ${_process} (non-fatal)"
}

_stage_in_skip() {
    case ",${SKIP_STAGES}," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

if ! _stage_in_skip backtest && ! _stage_in_skip parity; then
    _emit_heartbeat backtester
fi
if ! _stage_in_skip evaluator; then
    _emit_heartbeat evaluator
fi
