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
# Confirm-on-retry (2026-06-04): every check is sampled up to RETRY_ATTEMPTS
# times RETRY_DELAY apart, and only problems present in EVERY sample are
# reported. This kills the dominant false-positive class — a single-shot
# `ss -tln` on this busy box intermittently returns a TRUNCATED socket list,
# so a random subset of the five ports went missing for one probe and paged
# even though every service was provably up (root-caused from S3 dedup
# markers: 9 firings in one day, each naming a different random port subset,
# with zero corresponding service restarts). The same retry window also
# absorbs the 1-2s port gap during a deploy restart. A genuinely-down
# service/port (or a real PATH/tooling regression) stays missing across all
# samples and still pages on the first run. Confirmation adds latency only on
# the non-clean path; the common all-healthy case still exits after one cheap
# sample.
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
RETRY_ATTEMPTS=3                     # samples before a problem is confirmed
RETRY_DELAY=2                        # seconds between confirmation samples

# Resolve `ss` by absolute path once: it lives in /usr/sbin, which the systemd
# unit's PATH does not include, so a bare `ss` is "command not found" under the
# service. The script also sets PATH in the unit, so this is defense in depth.
SS_BIN=""
for cand in /usr/sbin/ss /sbin/ss /usr/bin/ss /bin/ss; do
    [ -x "$cand" ] && { SS_BIN="$cand"; break; }
done

# snapshot_problems — run the full check ONCE, printing one problem per line.
# No shared state; the caller samples it repeatedly and keeps the intersection.
snapshot_problems() {
    # memory headroom
    local mem_avail_mb
    mem_avail_mb=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo)
    if [ "${mem_avail_mb:-0}" -lt "$MEM_MIN_MB" ]; then
        echo "low memory: <${MEM_MIN_MB}MB available"
    fi

    # systemd services
    local s
    for s in "${SERVICES[@]}"; do
        systemctl is-active --quiet "$s" || echo "service down: $s"
    done

    # listening ports (mnemon/bun has no systemd unit here, so port is the probe).
    if [ -z "$SS_BIN" ]; then
        # Fail loud: a missing probe tool is a watchdog malfunction, NOT a port
        # outage. Reporting it distinctly stops a tooling/PATH regression from
        # masquerading as a fake all-ports-down alert (no-silent-fails). Persists
        # across samples, so it confirms and pages.
        echo "watchdog: ss probe unavailable (ss not found in /usr/sbin /sbin /usr/bin /bin)"
        return
    fi
    # Match ANY bind address: Streamlit binds 127.0.0.1:850x, but mnemon (bun)
    # binds *:8503, so an address-specific pattern false-alarms on 8503.
    local listening p
    listening=$("$SS_BIN" -tln 2>/dev/null)
    if [ -z "$listening" ]; then
        # Empty output from a present binary = probe failure, not 5 dead ports.
        # A transient empty read drops out on the next sample; a persistent one
        # confirms and pages.
        echo "watchdog: ss probe returned no output (cannot verify ports)"
        return
    fi
    for p in "${PORTS[@]}"; do
        echo "$listening" | grep -qE ":$p\b" || echo "port not listening: $p"
    done
}

# Confirm-on-retry: keep only problems present in EVERY sample. The common
# all-healthy path takes a single sample and exits without added latency.
confirmed=$(snapshot_problems)
if [ -z "$confirmed" ]; then
    exit 0
fi
attempt=1
while [ "$attempt" -lt "$RETRY_ATTEMPTS" ] && [ -n "$confirmed" ]; do
    sleep "$RETRY_DELAY"
    next=$(snapshot_problems)
    # intersection: lines present in BOTH the running set and this fresh sample
    confirmed=$(comm -12 <(printf '%s\n' "$confirmed" | sort) <(printf '%s\n' "$next" | sort))
    attempt=$((attempt + 1))
done

# all flagged problems self-healed within the confirmation window → no page
if [ -z "$confirmed" ]; then
    exit 0
fi

# Log the confirmed set so a firing is diagnosable from the journal directly
# (no S3 dedup-marker archaeology needed).
printf 'box_health: confirmed problems after %d samples:\n%s\n' "$attempt" "$confirmed" >&2

# build message + a dedup key derived from the problem set, so the same
# ongoing issue alerts once per dedup window rather than every 10 min.
mapfile -t problems <<< "$confirmed"
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
