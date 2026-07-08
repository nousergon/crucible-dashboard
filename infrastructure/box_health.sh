#!/bin/bash
# box_health.sh — lightweight resource + service watchdog for the shared
# dashboard EC2. The box runs ~5 web services
# (4 Streamlit + mnemon on bun) plus nginx on a small instance, so the
# binding constraint is RAM, not CPU. This alerts (deduped) when memory
# runs low or an expected service/port is down. Quiet on success.
#
# Co-resident services it guards (port -> service):
#   8501 dashboard.service        (alpha-engine console)
#   8502 nous-ergon-live.service  (live.nousergon.ai)
#   8503 mnemon (bun)             (memory.nousergon.ai)
#   8504 crucible-dash.service    (crucible.nousergon.ai/dash)
#   8505 signal.service           (signal.thecyphering.com)
#   8000 metron-api.service       (Metron FastAPI backend, internal)
#   3000 metron-web.service       (Metron Next.js, behind portfolio.nousergon.ai)
# (robodashboard.service / :8504 decommissioned 2026-06-10 — Metron succeeded it at
#  portfolio.nousergon.ai; robodashboard is now local-only. :8504 was reused by
#  crucible-dash.service on 2026-07-08 after the #354 deploy's port survey missed
#  that :8503 was already held by the mnemon/bun co-tenant — see config#1957,
#  crucible-dashboard#356, config#1972.)
#
# Confirm-on-retry (2026-06-04): every check is sampled up to RETRY_ATTEMPTS
# times RETRY_DELAY apart, and only problems present in EVERY sample are
# reported. This kills the dominant false-positive class — a single-shot
# `ss -tln` on this busy box intermittently returns a TRUNCATED socket list,
# so a random subset of the five ports went missing for one probe and paged
# even though every service was provably up (root-caused from S3 dedup
# markers: 9 firings in one day, each naming a different random port subset,
# with zero corresponding service restarts). The same retry window also
# absorbs the port gap during a deploy restart. A genuinely-down
# service/port (or a real PATH/tooling regression) stays missing across all
# samples and still pages on the first run. Confirmation adds latency only on
# the non-clean path; the common all-healthy case still exits after one cheap
# sample.
#
# Window sizing (2026-06-18): the window must exceed the SLOWEST guarded
# service's cold-start, or a deploy restart false-pages. The binding constraint
# is metron-api (uvicorn), which takes ~5s from `systemctl restart` to binding
# :8000 ("Application startup complete"). The original 3x2s (~4s) window was
# tuned for Streamlit's ~2s gap and was narrower than uvicorn's cold-start, so a
# manual `systemctl restart metron-api` landing just before a probe paged on
# "port not listening: 8000" even though the service came up seconds later (one
# such false page on 2026-06-18 during a Metron deploy). 4x4s (~12s) clears it
# with margin. Cost is paid only on the non-clean path, once per 10-min tick.
#
# Alerts go through krepis.alerts (SNS alpha-engine-alerts +
# Telegram), which dedups so a persistent problem only pages once per
# window. Installed to /usr/local/bin by install-box-health.sh; scheduled
# by box-health.timer (every 10 min).
set -uo pipefail

ENV_FILE="/home/ec2-user/.alpha-engine.env"
VENV_PY="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"

# Load Telegram creds etc. (SNS auth comes from the instance role).
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export AWS_REGION="${AWS_REGION:-us-east-1}"
# Self-discover this box's instance id (IMDSv2) for alert context — the box
# identifies itself rather than hardcoding the id. Degrade gracefully.
_imds_tok=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
INSTANCE_ID=$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: ${_imds_tok}" http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "dashboard-ec2")

# ── thresholds ──────────────────────────────────────────────────────────
MEM_MIN_MB=150                       # alert if MemAvailable drops below this
SERVICES=(dashboard.service nous-ergon-live.service crucible-dash.service signal.service metron-api.service metron-web.service)
PORTS=(8501 8502 8503 8504 8505 8000 3000)
RETRY_ATTEMPTS=4                     # samples before a problem is confirmed
RETRY_DELAY=4                        # seconds between confirmation samples (4x4s ~12s window > metron-api ~5s cold-start)

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
msg="dashboard EC2 (${INSTANCE_ID}) health alert:"
for p in "${problems[@]}"; do msg="$msg"$'\n'" - $p"; done
dkey="boxhealth-$(printf '%s' "${problems[*]}" | tr ' /' '__' | cut -c1-72)"

# krepis.alerts is the canonical CLI (config#1649): nousergon_lib.alerts is a
# re-export shim since lib v0.66.0 — guard-less under `python -m` on 0.81.0
# (silent exit-0 no-op, the config#1646 class). Invoke the real module.
"$VENV_PY" -m krepis.alerts publish \
    --message "$msg" \
    --severity warning \
    --source box-health \
    --dedup-key "$dkey" \
    --dedup-window-min 60 \
    || echo "box_health: alert publish failed" >&2
