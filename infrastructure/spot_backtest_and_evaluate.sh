#!/usr/bin/env bash
# infrastructure/spot_backtest_and_evaluate.sh
#
# Convenience wrapper: runs the backtester on a spot EC2 instance, then when
# that finishes successfully, triggers the evaluator on the always-on ae-data
# instance via SSM. Mirrors the Saturday Step Function's Backtester →
# Evaluator chain without re-running the upstream data/research/training
# steps.
#
# Use when you've landed a backtester or evaluator code change and want a
# fresh weekly report without paying for the upstream pipeline phases that
# already completed successfully on today's natural Saturday run.
#
# Usage:
#   bash infrastructure/spot_backtest_and_evaluate.sh
#   bash infrastructure/spot_backtest_and_evaluate.sh --mode simulate
#   bash infrastructure/spot_backtest_and_evaluate.sh --smoke-only
#     (skips evaluator since smoke mode produces no artifacts worth re-evaluating)
#
# Prerequisites:
#   - Everything spot_backtest.sh needs (AWS CLI, SSH key, .env, config.yaml)
#   - AWS_REGION and AE_DATA_INSTANCE_ID resolvable (defaults below)
#   - ae-data repo is on main with the evaluator code you want exercised

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"
AE_DATA_INSTANCE_ID="${AE_DATA_INSTANCE_ID:-i-09b539c844515d549}"
EVAL_MODE="${EVAL_MODE:-all}"

SMOKE_ONLY=0
for arg in "$@"; do
    if [ "$arg" = "--smoke-only" ]; then
        SMOKE_ONLY=1
    fi
done

# ── Step 1: Backtester on spot (blocks until complete) ───────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 1: Backtester (spot EC2)"
echo "═══════════════════════════════════════════════════════════════"
bash "$SCRIPT_DIR/spot_backtest.sh" "$@"
BACKTEST_RC=$?

if [ $BACKTEST_RC -ne 0 ]; then
    echo "Backtester failed (rc=$BACKTEST_RC). Skipping evaluator." >&2
    exit $BACKTEST_RC
fi

if [ $SMOKE_ONLY -eq 1 ]; then
    echo "Smoke-only mode — skipping evaluator step."
    exit 0
fi

# ── Step 2: Evaluator on always-on EC2 via SSM ───────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 2: Evaluator (ae-data via SSM)"
echo "═══════════════════════════════════════════════════════════════"

EVAL_CMD="set -eo pipefail; export HOME=/home/ec2-user ALPHA_ENGINE_DEPLOYED=1; sudo -u ec2-user git -C /home/ec2-user/alpha-engine-backtester pull --ff-only origin main; cd /home/ec2-user/alpha-engine-backtester; source .venv/bin/activate; python evaluate.py --mode ${EVAL_MODE} --upload 2>&1"

SSM_CMD_ID=$(aws ssm send-command \
    --instance-ids "$AE_DATA_INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "commands=[\"$EVAL_CMD\"]" \
    --timeout-seconds 900 \
    --region "$AWS_REGION" \
    --query "Command.CommandId" \
    --output text)

echo "SSM command launched: $SSM_CMD_ID"
echo "Polling for completion..."

# Poll every 15 s, up to 15 min total
for i in $(seq 1 60); do
    sleep 15
    STATUS=$(aws ssm get-command-invocation \
        --command-id "$SSM_CMD_ID" \
        --instance-id "$AE_DATA_INSTANCE_ID" \
        --region "$AWS_REGION" \
        --query "Status" \
        --output text 2>/dev/null || echo "Pending")
    echo "  [$i] status: $STATUS"
    case "$STATUS" in
        Success) break ;;
        Failed|Cancelled|TimedOut)
            echo "Evaluator SSM command failed: $STATUS" >&2
            aws ssm get-command-invocation \
                --command-id "$SSM_CMD_ID" \
                --instance-id "$AE_DATA_INSTANCE_ID" \
                --region "$AWS_REGION" \
                --query "StandardErrorContent" \
                --output text >&2 || true
            exit 1
            ;;
    esac
done

if [ "$STATUS" != "Success" ]; then
    echo "Evaluator timed out after 15 min" >&2
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Backtester + Evaluator complete. Check email for report."
echo "═══════════════════════════════════════════════════════════════"
