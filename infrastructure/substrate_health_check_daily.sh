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

cd /home/ec2-user/alpha-engine-dashboard

# ff-only, matching the SF's `git ... pull --ff-only origin main` exactly
# (no reset --hard embellishment) — a diverged checkout should fail loud
# here rather than silently rewrite history on a box other services share.
git pull --ff-only origin main

source .venv/bin/activate

# Ship the run log to S3 on exit — same trap the SF's Task ran inline.
trap 'aws s3 cp /var/log/substrate-health-check-daily.log "s3://alpha-engine-research/_ssm_logs/substrate-health-check-daily/$(date -u +%Y-%m-%d)/$(hostname)-$(date -u +%H%M%SZ).log" --only-show-errors || true' EXIT

python -m nousergon_lib.transparency --cadence daily --alert 2>&1 | tee /var/log/substrate-health-check-daily.log
