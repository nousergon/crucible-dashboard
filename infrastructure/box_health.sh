#!/bin/bash
# box_health.sh — lightweight resource + service watchdog for the shared
# dashboard EC2 (i-09b539c844515d549). The box runs ~5 web services
# (4 Streamlit + mnemon on bun) plus nginx on a small instance, so the
# binding constraint is RAM, not CPU. This alerts (deduped) when memory
# runs low or an expected service/port is down. Quiet on success.
#
# Co-resident services it guards (port -> service):
#   8501 dashboard.service        (alpha-engine console)
#   8502 nous-ergon-live.service  (live.nousergon.ai)
#   8503 mnemon (bun)             (memory.nousergon.ai)
#   8504 robodashboard.service    (portfolio.nousergon.ai)
#   8505 signal.service           (signal.thecyphering.com)
#
# Alerts go through alpha_engine_lib.alerts (SNS alpha-engine-alerts +
# Telegram), which dedups so a persistent problem only pages once per
# window. Installed to /usr/local/bin by install-box-health.sh; scheduled
# by box-health.timer (every 10 min).
set -uo pipefail

ENV_FILE="/home/ec2-user/.alpha-engine.env"
VENV_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"

# Load Telegram creds etc. (SNS auth comes from the instance role).
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export AWS_REGION="${AWS_REGION:-us-east-1}"

# ── thresholds ──────────────────────────────────────────────────────────
MEM_MIN_MB=150                       # alert if MemAvailable drops below this
SERVICES=(dashboard.service nous-ergon-live.service robodashboard.service signal.service)
PORTS=(8501 8502 8503 8504 8505)

problems=()

# memory headroom
mem_avail_mb=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo)
if [ "${mem_avail_mb:-0}" -lt "$MEM_MIN_MB" ]; then
    problems+=("low memory: ${mem_avail_mb}MB available (<${MEM_MIN_MB}MB)")
fi

# systemd services
for s in "${SERVICES[@]}"; do
    systemctl is-active --quiet "$s" || problems+=("service down: $s")
done

# listening ports (mnemon/bun has no systemd unit here, so port is the probe)
listening=$(ss -tln 2>/dev/null)
for p in "${PORTS[@]}"; do
    echo "$listening" | grep -qE "127\.0\.0\.1:$p\b" || problems+=("port not listening: $p")
done

# quiet on success — this is the common path
if [ "${#problems[@]}" -eq 0 ]; then
    exit 0
fi

# build message + a dedup key derived from the problem set, so the same
# ongoing issue alerts once per dedup window rather than every 10 min.
msg="dashboard EC2 (i-09b539c844515d549) health alert:"
for p in "${problems[@]}"; do msg="$msg"$'\n'" - $p"; done
dkey="boxhealth-$(printf '%s' "${problems[*]}" | tr ' /' '__' | cut -c1-72)"

"$VENV_PY" -m alpha_engine_lib.alerts publish \
    --message "$msg" \
    --severity warning \
    --source box-health \
    --dedup-key "$dkey" \
    --dedup-window-min 60 \
    || echo "box_health: alert publish failed" >&2
