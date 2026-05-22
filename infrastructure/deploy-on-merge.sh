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
# Ports sourced from the systemd unit files in this repo.
CONSOLE_URL="http://localhost:8501/_stcore/health"
LIVE_URL="http://localhost:8502/_stcore/health"

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
    # alpha-engine-lib is pinned @main ecosystem-wide so it must refresh
    # on every deploy regardless of requirements.txt diff.
    if grep -q 'alpha-engine-lib' requirements.txt 2>/dev/null; then
        sudo -u ec2-user .venv/bin/pip install --quiet --upgrade alpha-engine-lib 2>>"$LOG" \
            || log "WARN alpha-engine-lib upgrade failed (non-fatal)"
    fi
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

# ── 3. Restart both streamlit services (we are root) ───────────────────────
# Both services run from this same repo. Two-second stagger avoids a
# simultaneous blip on console + live site.
systemctl restart dashboard 2>>"$LOG" || fail "restart dashboard"
log "restarted dashboard.service"
sleep 2
systemctl restart nous-ergon-live 2>>"$LOG" || fail "restart nous-ergon-live"
log "restarted nous-ergon-live.service"

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

log "=== deploy-on-merge completed successfully — sha=$CURRENT_SHA ==="
exit 0
