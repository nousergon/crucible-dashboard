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
