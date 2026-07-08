#!/bin/bash
# deploy-on-merge.sh — Refresh lib, reload nginx on conf change,
# restart streamlit services, health check. Invoked via SSM (as root)
# from the dashboard deploy workflow AFTER the caller has already
# pulled the repo to the target SHA.
#
# The SSM command body owns the git pull (it must run before this
# script exists at the new path); this script owns everything after:
# pip / lib refresh, nginx config staging + reload on infrastructure/
# nginx.conf change, systemctl restart of both streamlit services, and
# the health check on both /_stcore/health endpoints.
#
# Boot-pull.sh remains the daily safety-net for ALL repos. This is the
# dashboard-specific fast-path so a PR merge becomes live in ~30s
# instead of waiting for the next 12:00-UTC boot-pull cycle.
#
# Usage (typically via SSM, not direct):
#   bash infrastructure/deploy-on-merge.sh <target-sha>

set -uo pipefail

REPO_DIR="/home/ec2-user/alpha-engine-dashboard"
LOG="/var/log/dashboard-deploy.log"
TARGET_SHA="${1:-HEAD}"

# Streamlit /_stcore/health endpoints. Console = 8501, live = 8502.
# Ports sourced from the systemd unit files in this repo. The live app
# sets baseUrlPath = "live" (live/.streamlit/config.toml, 2026-06-12 site
# cutover), which moves ALL its routes — including health — under /live.
CONSOLE_URL="http://localhost:8501/_stcore/health"
LIVE_URL="http://localhost:8502/live/_stcore/health"
DASH_URL="http://localhost:8504/dash/_stcore/health"
DASH_API_URL="http://localhost:8506/api/health"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }
fail() { log "FAIL $*"; exit 1; }

log "=== deploy-on-merge started — target=$TARGET_SHA ==="

cd "$REPO_DIR" || fail "cd $REPO_DIR"
CURRENT_SHA=$(sudo -u ec2-user git rev-parse HEAD)
log "repo HEAD -> $CURRENT_SHA"
log "$(sudo -u ec2-user git log --oneline -1)"

# ── 1. Refresh deps (as the owning user) ────────────────────────────────────
if [ -f ".venv/bin/pip" ] && [ -f "requirements.txt" ]; then
    # requirements.txt diff detection — pip install only on actual change.
    if sudo -u ec2-user git diff "${CURRENT_SHA}~1" "$CURRENT_SHA" -- requirements.txt 2>/dev/null | grep -q '^[+-]'; then
        log "requirements.txt changed — pip install"
        sudo -u ec2-user .venv/bin/pip install --quiet -r requirements.txt 2>>"$LOG" \
            || fail "pip install requirements.txt"
    fi
    # NOTE: nousergon-lib (renamed from alpha-engine-lib at v0.60.0) is
    # TAG-pinned in requirements.txt (@vX.Y.Z), not @main, so the
    # requirements.txt-diff-triggered install above is the correct and
    # sufficient refresh path — a version bump always changes requirements.txt.
    # A prior unconditional `pip install --upgrade alpha-engine-lib` block
    # lived here from the @main era; post-rename it matched only a stale
    # comment and tried to upgrade a dist name that no longer exists, emitting
    # a misleading "WARN alpha-engine-lib upgrade failed" every deploy. Removed.
fi

# ── 2. Reload nginx if infrastructure/nginx.conf changed ──────────────────
# Same conditional-on-diff pattern as the requirements.txt block above.
# nginx.conf is the source of truth for the routing layer (server_name +
# proxy_pass + sub_filter rules); previously a config edit required
# manually SSH'ing in to copy + reload. Now the fast-path auto-applies it.
#
# Order matters: nginx step runs BEFORE the streamlit restarts so a
# broken nginx config fails the deploy without bouncing streamlit.
NGINX_CONF_REPO="$REPO_DIR/infrastructure/nginx.conf"
NGINX_CONF_LIVE="/etc/nginx/conf.d/nousergon.conf"
if [ -f "$NGINX_CONF_REPO" ]; then
    if sudo -u ec2-user git diff "${CURRENT_SHA}~1" "$CURRENT_SHA" -- infrastructure/nginx.conf 2>/dev/null | grep -q '^[+-]'; then
        log "infrastructure/nginx.conf changed — staging + validating"
        # Stage to a tmp file, validate via nginx -t, then atomic-rename
        # into place. nginx -t against the staged file catches syntax
        # errors before the live file is touched.
        cp "$NGINX_CONF_REPO" "${NGINX_CONF_LIVE}.new" \
            || fail "cp nginx.conf staged"
        # nginx -t reads the entire conf.d/ tree; copy to a temp path under
        # conf.d/ would break the check. Instead, swap atomically then
        # validate; revert on failure.
        cp -p "$NGINX_CONF_LIVE" "${NGINX_CONF_LIVE}.bak" 2>/dev/null || true
        mv "${NGINX_CONF_LIVE}.new" "$NGINX_CONF_LIVE" \
            || fail "mv nginx.conf into place"
        if ! nginx -t 2>>"$LOG"; then
            log "FAIL nginx -t after staging new conf — reverting"
            if [ -f "${NGINX_CONF_LIVE}.bak" ]; then
                mv "${NGINX_CONF_LIVE}.bak" "$NGINX_CONF_LIVE" \
                    || log "WARN nginx.conf revert mv failed"
                nginx -t >>"$LOG" 2>&1 || log "WARN nginx -t still failing after revert"
            fi
            fail "nginx -t (new conf rejected)"
        fi
        rm -f "${NGINX_CONF_LIVE}.bak"
        systemctl reload nginx 2>>"$LOG" || fail "systemctl reload nginx"
        log "reloaded nginx with updated nousergon.conf"
    fi
fi

# ── 2b. Re-install morning-signal watchdog if its wrapper/units changed ────
# The freshness watchdog is a /usr/local/bin wrapper + systemd units installed
# OUT of the repo tree by install-morning-signal-watchdog.sh. Unlike the
# streamlit services (restarted every deploy) it had NO auto-deploy, so a repo
# edit silently failed to reach the box. That bit us: the wrapper's alert-publish
# call was migrated alpha_engine_lib.alerts -> nousergon_lib.alerts in the repo,
# but the installed /usr/local/bin copy stayed on the old name and crashed
# (`_AliasLoader` has no `get_code`) — so a real "episode missing" event went
# unpaged (morning-signal#77). Same conditional-on-diff pattern as nginx above:
# re-run the idempotent installer ONLY when the wrapper, its installer, or its
# units changed in this merge.
WATCHDOG_PATHS="infrastructure/morning-signal-watchdog.sh infrastructure/install-morning-signal-watchdog.sh infrastructure/systemd/morning-signal-watchdog.service infrastructure/systemd/morning-signal-watchdog.timer"
if sudo -u ec2-user git diff "${CURRENT_SHA}~1" "$CURRENT_SHA" -- $WATCHDOG_PATHS 2>/dev/null | grep -q '^[+-]'; then
    log "morning-signal watchdog wrapper/units changed — re-installing"
    bash "$REPO_DIR/infrastructure/install-morning-signal-watchdog.sh" >>"$LOG" 2>&1 \
        || fail "install-morning-signal-watchdog.sh"
    log "re-installed morning-signal watchdog"
fi

# ── 2c. Re-install morning-signal core units if they changed ───────────────
# The morning-signal service/timer/drop-ins + the generate-only recovery
# wrapper are box-provisioned out of the repo tree by install-morning-signal.sh
# (they used to be unmanaged box-only units — morning-signal#79). Same
# conditional-on-diff auto-deploy as the watchdog block above so a unit edit
# (e.g. the Requires=->Wants= daily-news fix, morning-signal#78) actually
# reaches the box instead of silently drifting.
MS_UNIT_PATHS="infrastructure/systemd/morning-signal.service infrastructure/systemd/morning-signal.timer infrastructure/systemd/morning-signal.service.d infrastructure/install-morning-signal.sh infrastructure/morning-signal-recover.sh"
if sudo -u ec2-user git diff "${CURRENT_SHA}~1" "$CURRENT_SHA" -- $MS_UNIT_PATHS 2>/dev/null | grep -q '^[+-]'; then
    log "morning-signal core units/recovery wrapper changed — re-installing"
    bash "$REPO_DIR/infrastructure/install-morning-signal.sh" >>"$LOG" 2>&1 \
        || fail "install-morning-signal.sh"
    log "re-installed morning-signal core units"
fi

# ── 2d. Re-install morning-signal OSS bakeoff units if they changed ────────
# The weekly Phase B shadow-bakeoff (config#1659) timer/service, same
# conditional-on-diff auto-deploy as 2b/2c above -- including on FIRST
# introduction, since a brand-new file counts as a diff (`+` lines), so this
# also handles the initial rollout without a manual on-box step.
BAKEOFF_UNIT_PATHS="infrastructure/systemd/morning-signal-bakeoff.service infrastructure/systemd/morning-signal-bakeoff.timer infrastructure/install-morning-signal-bakeoff.sh"
if sudo -u ec2-user git diff "${CURRENT_SHA}~1" "$CURRENT_SHA" -- $BAKEOFF_UNIT_PATHS 2>/dev/null | grep -q '^[+-]'; then
    log "morning-signal bakeoff units changed — re-installing"
    bash "$REPO_DIR/infrastructure/install-morning-signal-bakeoff.sh" >>"$LOG" 2>&1 \
        || fail "install-morning-signal-bakeoff.sh"
    log "re-installed morning-signal bakeoff units"
fi

# ── 3. Restart both streamlit services (we are root) ───────────────────────
# Both services run from this same repo. Two-second stagger avoids a
# simultaneous blip on console + live site.
systemctl restart dashboard 2>>"$LOG" || fail "restart dashboard"
log "restarted dashboard.service"
sleep 2
systemctl restart nous-ergon-live 2>>"$LOG" || fail "restart nous-ergon-live"
log "restarted nous-ergon-live.service"

# ── 3b. Crucible /dash service (config#1957) — idempotent self-provision ────
# The unit ships in this repo; install/refresh it on unit-file diff (or first
# deploy) so the service can never drift from the repo copy — mirrors the CF
# Pages project self-provision precedent (#328): a new box or a unit change
# needs no manual step.
DASH_UNIT_SRC="$REPO_DIR/infrastructure/crucible-dash.service"
DASH_UNIT_DST="/etc/systemd/system/crucible-dash.service"
if [ ! -f "$DASH_UNIT_DST" ] || ! cmp -s "$DASH_UNIT_SRC" "$DASH_UNIT_DST"; then
    cp "$DASH_UNIT_SRC" "$DASH_UNIT_DST" 2>>"$LOG" || fail "install crucible-dash unit"
    systemctl daemon-reload 2>>"$LOG" || fail "daemon-reload for crucible-dash"
    systemctl enable crucible-dash 2>>"$LOG" || fail "enable crucible-dash"
    log "installed/refreshed crucible-dash.service unit"
fi
sleep 2
systemctl restart crucible-dash 2>>"$LOG" || fail "restart crucible-dash"
log "restarted crucible-dash.service"

# ── 3c. Crucible dash-api service (config#1973 9-B) — same idempotent
# self-provision pattern as 3b.
API_UNIT_SRC="$REPO_DIR/infrastructure/crucible-dash-api.service"
API_UNIT_DST="/etc/systemd/system/crucible-dash-api.service"
if [ ! -f "$API_UNIT_DST" ] || ! cmp -s "$API_UNIT_SRC" "$API_UNIT_DST"; then
    cp "$API_UNIT_SRC" "$API_UNIT_DST" 2>>"$LOG" || fail "install crucible-dash-api unit"
    systemctl daemon-reload 2>>"$LOG" || fail "daemon-reload for crucible-dash-api"
    systemctl enable crucible-dash-api 2>>"$LOG" || fail "enable crucible-dash-api"
    log "installed/refreshed crucible-dash-api.service unit"
fi
sleep 1
systemctl restart crucible-dash-api 2>>"$LOG" || fail "restart crucible-dash-api"
log "restarted crucible-dash-api.service"

# ── 4. Health check ─────────────────────────────────────────────────────────
# Streamlit's /_stcore/health returns 200 OK with body "ok" once the
# server is ready. Give it up to 30s per service to bind the port.
wait_for_health() {
    local url="$1"
    local label="$2"
    local n=0
    while [ $n -lt 30 ]; do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            log "OK   $label — health passed at t=${n}s"
            return 0
        fi
        sleep 1
        n=$((n + 1))
    done
    log "FAIL $label — health check timed out after 30s"
    return 1
}

wait_for_health "$CONSOLE_URL" "dashboard (console)" || fail "console health"
wait_for_health "$LIVE_URL" "nous-ergon-live" || fail "live health"
wait_for_health "$DASH_URL" "crucible-dash" || fail "crucible-dash health"
wait_for_health "$DASH_API_URL" "crucible-dash-api" || fail "crucible-dash-api health"

log "=== deploy-on-merge completed successfully — sha=$CURRENT_SHA ==="
exit 0
