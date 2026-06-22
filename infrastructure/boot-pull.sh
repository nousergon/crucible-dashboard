#!/bin/bash
# boot-pull.sh — Pull latest code for all Alpha Engine repos on the micro EC2.
#
# Runs as a systemd oneshot service, triggered by a daily timer at 12:00 UTC
# (5am PDT / 4am PST). Also runnable manually:
#
#   sudo systemctl start boot-pull
#
# Why a timer instead of on-boot?
# The micro is always-on (24/7). The timer bounds drift to ≤24h regardless
# of whether the instance reboots. 5am PT / 12:00 UTC was chosen because it
# runs before Brian wakes up so any failure is visible in the morning and
# can be addressed before the weekday Saturday pipeline fires at 5 PM PT.
#
# Mirrors the trading instance's boot-pull.sh (alpha-engine/infrastructure/)
# with a different REPOS array.

set -uo pipefail

LOG="/var/log/boot-pull.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

log "=== boot-pull started ==="

# ── Refresh the GitHub PAT in ~/.netrc from SSM ────────────────────────────
# alpha-engine-config is the only PRIVATE repo pulled below; git authenticates
# to it over HTTPS via the fine-grained PAT in ~/.netrc (libcurl reads ~/.netrc
# by default). That token used to be hand-copied onto each box, so a PAT
# rotation silently broke every box's private-repo pull until someone re-pasted
# it. 2026-06-03 incident: the executor PAT was rotated, this box's stale
# ~/.netrc (mtime Mar 9) started returning 401, and boot-pull FAILed on
# alpha-engine-config with "could not read Username".
#
# /alpha-engine/GITHUB_TOKEN (SecureString) is now the single source of truth.
# Hydrating ~/.netrc from it on every run means a future rotation only needs an
# SSM update — it auto-propagates to every box within one boot-pull cycle, the
# same self-bootstrapping pattern as the SSM-hydrated config.yaml files below.
#
# Best-effort by design (per ~/Development/CLAUDE.md item 3 — fail-loud): a
# refresh failure here is WARN-only and MUST NOT clobber a working ~/.netrc,
# because (a) the on-disk token may still be valid, and (b) the REAL failure
# mode — alpha-engine-config unfetchable — is already surfaced loudly by the
# FAILED_REPOS → flow-doctor report at the end of this script. We only
# overwrite ~/.netrc when SSM hands back a non-empty token, so a transient SSM
# blip can never wipe valid credentials.
GH_USER="cipher813"
NETRC="/home/ec2-user/.netrc"
if GH_TOKEN=$(aws ssm get-parameter --name /alpha-engine/GITHUB_TOKEN \
        --with-decryption --query "Parameter.Value" --output text 2>>"$LOG") \
        && [ -n "$GH_TOKEN" ] && [ "$GH_TOKEN" != "None" ]; then
    NEW_NETRC="machine github.com login ${GH_USER} password ${GH_TOKEN}"
    if [ ! -f "$NETRC" ] || [ "$NEW_NETRC" != "$(cat "$NETRC" 2>/dev/null)" ]; then
        # umask 077 + atomic tmp→mv so the token never lands in a
        # world-readable or half-written file.
        ( umask 077; printf '%s\n' "$NEW_NETRC" > "${NETRC}.tmp.$$" )
        mv "${NETRC}.tmp.$$" "$NETRC"
        chmod 600 "$NETRC"
        log "OK   ~/.netrc refreshed from SSM /alpha-engine/GITHUB_TOKEN"
    else
        log "OK   ~/.netrc unchanged from SSM"
    fi
    unset GH_TOKEN NEW_NETRC
else
    log "WARN ~/.netrc refresh skipped — SSM /alpha-engine/GITHUB_TOKEN unreadable/empty; keeping existing ~/.netrc (private-repo pull will FAIL-loud below if the on-disk token is also stale)"
fi

# Repos the micro needs at runtime. Order matters only for dependency
# (alpha-engine-config first so other repos can reference it on pull).
# robodashboard is the 3rd Streamlit service on this shared box; it has its own
# merge-deploy GHA (the fast path) — boot-pull is its cross-repo safety net so a
# failed GHA run still gets code synced (and the app restarted) within ≤1 day.
REPOS=(
    /home/ec2-user/alpha-engine-config
    /home/ec2-user/alpha-engine-data
    /home/ec2-user/alpha-engine-research
    /home/ec2-user/alpha-engine-dashboard
    /home/ec2-user/robodashboard
    /home/ec2-user/flow-doctor
)

PULL_FAILURES=0
FAILED_REPOS=()
ROBODASH_CHANGED=0

for repo in "${REPOS[@]}"; do
    if [ ! -d "$repo/.git" ]; then
        log "SKIP $repo (not cloned)"
        continue
    fi

    log "Pulling $repo ..."
    cd "$repo"
    PREV_SHA=$(git rev-parse HEAD 2>/dev/null || echo "none")
    if git fetch origin >> "$LOG" 2>&1 && git reset --hard origin/main >> "$LOG" 2>&1; then
        NEW_SHA=$(git rev-parse HEAD 2>/dev/null || echo "none")
        log "OK   $repo — $(git log --oneline -1)"
        # Track robodashboard code changes so we can restart its Streamlit app
        # below (its unit files aren't in this repo's systemd sync section).
        if [ "$repo" = "/home/ec2-user/robodashboard" ] && [ "$PREV_SHA" != "$NEW_SHA" ]; then
            ROBODASH_CHANGED=1
        fi

        # Only run full pip install if requirements.txt actually changed — pip
        # is slow on a 1GB instance and runs every day even when no deps moved.
        #
        # EXCLUDE alpha-engine-data: on this box it runs ONLY the slim daily-news
        # collector (managed by daily-news.service, which installs
        # requirements-daily-news.txt into its own .venv). A full
        # `pip install -r requirements.txt` here would pull the heavy data stack
        # (arcticdb/voyageai/edgartools, ~1.5 GB) into that slim venv and risk
        # filling the shared t3.small's disk. The daily-news wrapper owns its
        # slim deps; boot-pull still git-syncs the repo (reset --hard above).
        if [ "$repo" != "/home/ec2-user/alpha-engine-data" ] && \
           [ "$PREV_SHA" != "$NEW_SHA" ] && [ -f "requirements.txt" ] && [ -f ".venv/bin/pip" ]; then
            if git diff "$PREV_SHA" "$NEW_SHA" -- requirements.txt | grep -q "^[+-]"; then
                log "GATE $repo — requirements.txt changed, running pip install"
                if .venv/bin/pip install --quiet -r requirements.txt >> "$LOG" 2>&1; then
                    log "OK   $repo — deps updated"
                else
                    log "FAIL $repo — pip install failed"
                    PULL_FAILURES=$((PULL_FAILURES + 1))
                    FAILED_REPOS+=("$repo (pip)")
                fi
            fi
        fi

        # NOTE (2026-06-11): two legacy blocks removed here — see git history.
        # (1) "Always refresh alpha-engine-lib" dated from the @main-pin era;
        # the fleet pins stable tags now (@main is CI-forbidden), so a daily
        # `pip install --upgrade alpha-engine-lib` VIOLATED every repo's pin by
        # pulling latest PyPI (it had been failing daily on venv remnants and
        # WARN-swallowing — the requirements-diff GATE above is the one
        # correct dep path: venv changes exactly when the pin changes).
        # (2) the flow-doctor editable-install override (stale local clone was
        # serving rc3 over the lib-pinned rc5) — the trading box's boot-pull
        # removed this pattern for the same reason; flow-doctor arrives
        # transitively via alpha-engine-lib[flow_doctor].
    else
        log "FAIL $repo — fetch/reset failed"
        PULL_FAILURES=$((PULL_FAILURES + 1))
        FAILED_REPOS+=("$repo (git)")
    fi
done

# ── Hydrate gitignored config files from SSM Parameter Store ───────────────
# The canonical source of truth for the dashboard's two config.yaml files
# is AWS SSM (since 2026-05-21). Boot-pull fetches them on every run so
# a fresh EC2 + cloned repo + boot-pull = fully self-bootstrapping; the
# repo is git-only, no orphaned local files needed for the Streamlit apps
# to start.
#
# Fail-loud (per ~/Development/CLAUDE.md item 3): missing or empty
# parameters MUST hard-fail; never let Streamlit start with a stale or
# placeholder config. The .example files in the repo are NOT runtime
# fallbacks (per [[example-files-never-in-prod-config-search-paths]]).
fetch_config_from_ssm() {
    local ssm_name="$1"
    local target="$2"
    local content
    if ! content=$(aws ssm get-parameter --name "$ssm_name" \
            --query "Parameter.Value" --output text 2>>"$LOG"); then
        log "FAIL SSM get-parameter $ssm_name — aws CLI errored"
        return 1
    fi
    if [ -z "$content" ] || [ "$content" = "None" ]; then
        log "FAIL SSM $ssm_name returned empty (refusing to write empty config)"
        return 1
    fi
    # Diff-against-on-disk so we only rewrite (and trigger restart) on
    # actual change. Avoids spurious restart-during-boot-pull churn.
    if [ -f "$target" ] && [ "$content" = "$(cat "$target")" ]; then
        log "OK   $target unchanged from SSM"
        return 0
    fi
    # Atomic write via tmp + mv so a partial-write can't leave a half-baked
    # config on disk if the process is killed between truncate and full write.
    local tmp="${target}.ssm-tmp.$$"
    printf '%s' "$content" > "$tmp"
    sudo -u ec2-user mv "$tmp" "$target"
    sudo chown ec2-user:ec2-user "$target"
    log "OK   $target updated from SSM ($ssm_name)"
    CONFIGS_CHANGED=1
}

CONFIGS_CHANGED=0
if ! fetch_config_from_ssm /alpha-engine/dashboard/config.yaml \
        /home/ec2-user/alpha-engine-dashboard/config.yaml; then
    log "FAIL boot-pull aborting — could not fetch /alpha-engine/dashboard/config.yaml"
    PULL_FAILURES=$((PULL_FAILURES + 1))
    FAILED_REPOS+=("ssm:config.yaml")
fi
if ! fetch_config_from_ssm /alpha-engine/dashboard/live-config.yaml \
        /home/ec2-user/alpha-engine-dashboard/live/config.yaml; then
    log "FAIL boot-pull aborting — could not fetch /alpha-engine/dashboard/live-config.yaml"
    PULL_FAILURES=$((PULL_FAILURES + 1))
    FAILED_REPOS+=("ssm:live-config.yaml")
fi

# ── Sync systemd unit files from dashboard repo ─────────────────────────────
# The source of truth for unit files is the repo. This reloads systemd and
# restarts any service whose unit file actually changed, so drift between
# the repo and /etc/systemd/system is bounded to ≤1 day.
SYSTEMD_SRC="/home/ec2-user/alpha-engine-dashboard/infrastructure/systemd"
if [ -d "$SYSTEMD_SRC" ]; then
    CHANGED_UNITS=()
    for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
        [ -f "$unit" ] || continue
        name=$(basename "$unit")
        if [ -f "/etc/systemd/system/$name" ]; then
            if ! diff -q "$unit" "/etc/systemd/system/$name" >/dev/null 2>&1; then
                sudo cp "$unit" "/etc/systemd/system/$name"
                log "SYNC $name (updated)"
                CHANGED_UNITS+=("$name")
            fi
        else
            sudo cp "$unit" "/etc/systemd/system/$name"
            log "SYNC $name (new)"
            CHANGED_UNITS+=("$name")
        fi
    done
    if [ ${#CHANGED_UNITS[@]} -gt 0 ]; then
        sudo systemctl daemon-reload
        log "systemctl daemon-reload"
        # Restart changed services. Timers will re-schedule themselves on
        # daemon-reload automatically.
        for unit in "${CHANGED_UNITS[@]}"; do
            if [[ "$unit" == *.service ]] && [ "$unit" != "boot-pull.service" ]; then
                sudo systemctl restart "$unit" 2>> "$LOG" || log "WARN restart $unit failed"
                log "RESTART $unit"
            fi
        done
    fi
fi

# ── Restart streamlit services if SSM-hydrated configs changed ─────────────
# Streamlit reads config.yaml at module import (decorator evaluation in
# loaders/s3_loader.py via @st.cache_data(ttl=_ttl("trades"))). A config
# change therefore requires a full process restart; reloading streamlit
# secrets via the .streamlit/ path is not sufficient.
if [ "$CONFIGS_CHANGED" -eq 1 ]; then
    log "CONFIGS_CHANGED=1 — restarting streamlit services"
    sudo systemctl restart dashboard 2>> "$LOG" || log "WARN restart dashboard failed"
    sleep 2
    sudo systemctl restart nous-ergon-live 2>> "$LOG" || log "WARN restart nous-ergon-live failed"
    log "RESTART dashboard + nous-ergon-live (config-driven)"
fi

# ── Restart robodashboard's Streamlit app if its code changed this run ──────
# Safety net for robodashboard's own merge-deploy GHA (the fast path that
# restarts on every merge). If that GHA failed, the reset --hard above still
# synced robodashboard's code within ≤1 day; restart the long-running Streamlit
# service so it actually picks it up. Its unit files are owned by robodashboard's
# own deploy, so they are NOT in the systemd-sync section above; the
# snapshot timer re-reads code per run, so only this service needs a restart.
# robodashboard.service's ExecStartPre re-hydrates its secrets/config, so the
# restart is self-contained. Best-effort (WARN-only) like the others.
if [ "$ROBODASH_CHANGED" -eq 1 ]; then
    sudo systemctl restart robodashboard 2>> "$LOG" || log "WARN restart robodashboard failed"
    log "RESTART robodashboard (code-driven safety net)"
fi

# ── Report failures to flow-doctor if any occurred ──────────────────────────
# Don't rely on the log file alone — flow-doctor's GitHub notifier gives a
# visible red badge on the repo so the failure isn't invisible in
# /var/log/boot-pull.log until someone happens to look.
if [ "$PULL_FAILURES" -gt 0 ]; then
    log "=== boot-pull completed with $PULL_FAILURES failure(s): ${FAILED_REPOS[*]} ==="
    # Fire-and-forget report. If flow-doctor itself is broken, the log
    # above is the fallback signal.
    FD_VENV="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"
    if [ -x "$FD_VENV" ]; then
        "$FD_VENV" - <<PYEOF 2>> "$LOG" || true
import os
import sys
sys.path.insert(0, "/home/ec2-user/alpha-engine-dashboard")
try:
    from nousergon_lib.secrets import get_secret
    for _name in ("EMAIL_SENDER", "EMAIL_RECIPIENTS", "GMAIL_APP_PASSWORD", "FLOW_DOCTOR_GITHUB_TOKEN"):
        _val = get_secret(_name, required=False)
        if _val is not None and _name not in os.environ:
            os.environ[_name] = _val
    import flow_doctor
    fd = flow_doctor.init(
        config_path="/home/ec2-user/alpha-engine-dashboard/flow-doctor.yaml",
    )
    fd.report(
        RuntimeError("boot-pull failed: ${FAILED_REPOS[*]}"),
        severity="error",
        context={"site": "boot-pull", "failures": "${FAILED_REPOS[*]}"},
    )
except Exception as e:
    print(f"[boot-pull] flow-doctor report failed: {e}", file=sys.stderr)
PYEOF
    fi
    exit 1
fi

log "=== boot-pull completed successfully ==="
exit 0
