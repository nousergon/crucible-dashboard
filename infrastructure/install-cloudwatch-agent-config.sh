#!/bin/bash
# install-cloudwatch-agent-config.sh — install the host-metrics agent config
# and the OOM-kill collector on the dashboard box.
#
# Before 2026-07-27 the agent collected only disk used_percent, and its config
# existed only on the box. Both are fixed here: memory/swap are collected (the
# actual binding constraint), and the config is version-controlled so a rebuild
# reproduces it.
#
# Usage:  sudo ./infrastructure/install-cloudwatch-agent-config.sh
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T0-3

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_SRC="$HERE/cloudwatch-agent.json"
CFG_DST="/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.d/file_amazon-cloudwatch-agent.json"
CTL="/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl"

[[ -f "$CFG_SRC" ]] || { echo "missing $CFG_SRC" >&2; exit 1; }
python3 -c "import json,sys; json.load(open('$CFG_SRC'))" || {
    echo "$CFG_SRC is not valid JSON" >&2; exit 1; }

echo "==> installing CloudWatch agent config"
install -m 0644 "$CFG_SRC" "$CFG_DST"

# fetch-config re-reads the .d directory and restarts the agent.
"$CTL" -a fetch-config -m ec2 -s -c "file:${CFG_DST}"

echo "==> installing OOM-kill collector"
install -m 0755 "$HERE/emit_oom_metric.sh" /usr/local/bin/emit_oom_metric.sh
install -m 0644 "$HERE/systemd/emit-oom-metric.service" /etc/systemd/system/
install -m 0644 "$HERE/systemd/emit-oom-metric.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now emit-oom-metric.timer

echo "==> priming the OOM baseline (first run establishes the counter)"
systemctl start emit-oom-metric.service || {
    echo "emit-oom-metric.service failed on first run -- investigate before" >&2
    echo "trusting the OOMKills metric" >&2
    exit 1; }

echo "==> installing per-service memory collector (alpha-engine-config-I7804)"
install -m 0755 "$HERE/emit_service_memory.sh" /usr/local/bin/emit_service_memory.sh
install -m 0644 "$HERE/systemd/emit-service-memory.service" /etc/systemd/system/
install -m 0644 "$HERE/systemd/emit-service-memory.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now emit-service-memory.timer

# Same fail-loud first run as the OOM collector above, and for a sharper
# reason: this one reads its unit list out of budget.yaml, so a change to that
# file's shape breaks the collector while leaving the timer green-looking.
# The first run is the only place that failure is cheap to see.
echo "==> priming the per-service memory series (first run proves it can read budget.yaml)"
systemctl start emit-service-memory.service || {
    echo "emit-service-memory.service failed on first run -- investigate before" >&2
    echo "trusting the ServiceMemoryMiB metric; check that budget.yaml is" >&2
    echo "readable at the path the script defaults to" >&2
    exit 1; }

echo
echo "Done. Verify:"
echo "  $CTL -a status"
echo "  systemctl list-timers emit-oom-metric.timer emit-service-memory.timer"
echo "  journalctl -u emit-oom-metric.service -n 5 --no-pager"
echo "  journalctl -u emit-service-memory.service -n 5 --no-pager"
echo
echo "Metrics appear in namespace AlphaEngine/Host within ~5 min:"
echo "  mem_available_percent, mem_used_percent, mem_available,"
echo "  swap_used_percent, disk used_percent, OOMKills, OOMKillsTotal,"
echo "  ServiceMemoryMiB (per Unit), ServiceMemoryTotalMiB"
