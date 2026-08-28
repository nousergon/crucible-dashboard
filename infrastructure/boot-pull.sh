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

# Alerts publish through the DECLARED krepis venv, not through whichever
# checkout happened to carry a krepis (config-I7168).
#
# TWO candidate paths, because three of these scripts are INSTALLED to
# /usr/local/bin and run from there, where no sibling exists. Resolving only
# alongside $BASH_SOURCE is what broke box-health within a minute of deploying
# I7168: `/usr/local/bin/box_health.sh: line 1215: ALERT_PY: unbound variable`,
# every 10 minutes, on the box's primary watchdog. The file was correct; the
# DEPLOY PATH was not, and no amount of reading the file shows that.
#
# The final `:=` is the load-bearing line. Whatever happens above it, ALERT_PY
# is set — an alerting path that cannot start is strictly worse than one on an
# older krepis, and `set -u` turns an unset variable into a dead watchdog.
for _ap in "$(dirname "${BASH_SOURCE[0]}")/alert_py.sh" \
           /home/ec2-user/alpha-engine-dashboard/infrastructure/alert_py.sh; do
    if [ -r "$_ap" ]; then . "$_ap"; break; fi
done
unset _ap
: "${ALERT_PY:=/home/ec2-user/alpha-engine-dashboard/.venv/bin/python}"

# Same two-candidate resolution as ALERT_PY above (this script runs in
# place, not snapshotted, but mirrors the pattern rather than assuming
# $BASH_SOURCE always resolves — see alert_py.sh's comment for why).
for _gsl in "$(dirname "${BASH_SOURCE[0]}")/lib/git-sync-lock.sh" \
            /home/ec2-user/alpha-engine-dashboard/infrastructure/lib/git-sync-lock.sh; do
    if [ -r "$_gsl" ]; then . "$_gsl"; break; fi
done
unset _gsl

LOG="/var/log/boot-pull.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

log "=== boot-pull started ==="

# ── T1-3 deploy accounting (shared-application-host-policy §5 T1-3, config-I6742) ──
# boot-pull mutates code on a live host, so it is held to the same floor as
# deploy-on-merge.sh: the SHA each repo moved to is RECORDED to a state file,
# every service this run restarts is HEALTH-CHECKED afterward, and a failed
# gate REVERTS this run's pulls to their previous SHAs. Unlike deploy-on-merge
# (which needs a last-good stamp file because its failing HEAD is the new
# merge), boot-pull captures PREV_SHA before each pull — the tree the box was
# demonstrably running on until this run — so the revert target is exact.
LAST_PULL_SHAS="/var/lib/boot-pull/last-pull-shas"
PULLED_REPOS=()
PULLED_PREV=()
PULLED_NEW=()
RESTARTED_SERVICES=()

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
# robodashboard (the prior 3rd Streamlit service on this box) was decommissioned
# 2026-07-01 in favor of Metron — see nousergon/metron-ops#119. Metron has its own
# merge-deploy GHA but is NOT yet in this safety-net loop (its pip-editable +
# npm-build install shape doesn't fit this REPOS-array pattern) — tracked as a
# follow-up, not silently dropped.
REPOS=(
    /home/ec2-user/alpha-engine-config
    /home/ec2-user/alpha-engine-data
    /home/ec2-user/alpha-engine-research
    /home/ec2-user/alpha-engine-dashboard
    /home/ec2-user/flow-doctor
)

PULL_FAILURES=0
FAILED_REPOS=()

for repo in "${REPOS[@]}"; do
    if [ ! -d "$repo/.git" ]; then
        log "SKIP $repo (not cloned)"
        continue
    fi

    log "Pulling $repo ..."
    cd "$repo"
    PREV_SHA=$(git rev-parse HEAD 2>/dev/null || echo "none")
    # Per-checkout flock (config incident 2026-08-27 20:07 UTC, see
    # infrastructure/lib/git-sync-lock.sh): this loop is one of several
    # unsynchronised writers against $repo (deploy.yml,
    # substrate_health_check_daily.sh also touch alpha-engine-dashboard),
    # and a fetch is itself a git WRITE — it mutates the remote-tracking
    # ref — so it must take the lock too, not just the reset.
    _repo_lock="$(git_sync_lock_path "$repo")"
    if flock -w "$GIT_SYNC_LOCK_WAIT" "$_repo_lock" bash -c 'git fetch origin && git reset --hard origin/main' >> "$LOG" 2>&1; then
        NEW_SHA=$(git rev-parse HEAD 2>/dev/null || echo "none")
        log "OK   $repo — $(git log --oneline -1)"
        if [ "$PREV_SHA" != "$NEW_SHA" ]; then
            # This run moved the tree — remember where from, for the T1-3
            # health-gate revert below.
            PULLED_REPOS+=("$repo")
            PULLED_PREV+=("$PREV_SHA")
            PULLED_NEW+=("$NEW_SHA")
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
           [ "$PREV_SHA" != "$NEW_SHA" ] && [ -f "requirements.txt" ] && [ -x ".venv/bin/python" ]; then
            if git diff "$PREV_SHA" "$NEW_SHA" -- requirements.txt | grep -q "^[+-]"; then
                log "GATE $repo — requirements.txt changed, running pip install"

                # TMPDIR fix (config#2792/#2736, 2026-07-17, mirrors
                # deploy-on-merge.sh): pip has no dedicated scratch-space
                # option — it always uses tempfile.gettempdir(), i.e. $TMPDIR
                # or /tmp. This box's /tmp is a 957MB tmpfs shared across ALL
                # repos in this loop, while / has 16G+ free. A full-closure
                # install can overflow that small tmpfs with
                # `OSError: [Errno 28] No space left on device` even though
                # the real root disk stays healthy — live SSM reproduction on
                # i-09b539c844515d549 confirmed the identical install
                # succeeds once TMPDIR points at the root filesystem instead.
                # Scoped per-repo (not one shared dir) so two loop iterations
                # can never collide.
                PIP_TMPDIR="${repo}/.pip-tmp"
                rm -rf "$PIP_TMPDIR"
                mkdir -p "$PIP_TMPDIR"

                # Pre-pip fail-loud disk guards — same two risk classes as
                # deploy-on-merge.sh: (a) TMPDIR's own filesystem filling
                # (the current failure mode), (b) root "/" filling (the
                # config#2227 class, still real if PIP_TMPDIR ever moves off
                # "/"). Self-classifying from $LOG alone.
                GUARD_FAIL=0
                for guard_path in "$PIP_TMPDIR" "/"; do
                    guard_pcent="$(df --output=pcent "$guard_path" 2>/dev/null | tail -1 | tr -dc '0-9')"
                    guard_mount="$(df --output=target "$guard_path" 2>/dev/null | tail -1 | tr -d ' ')"
                    if [ -n "$guard_pcent" ] && [ "$guard_pcent" -ge 90 ]; then
                        log "FAIL $repo — DISK FULL — ${guard_pcent}% used on ${guard_mount:-$guard_path}, pip install aborted before running (config#2227/#2792/#2736 class; a rerun cannot heal this without freeing space)"
                        GUARD_FAIL=1
                    fi
                done

                if [ "$GUARD_FAIL" -eq 1 ]; then
                    PULL_FAILURES=$((PULL_FAILURES + 1))
                    FAILED_REPOS+=("$repo (disk-full)")
                elif TMPDIR="$PIP_TMPDIR" .venv/bin/python -m pip install --quiet -r requirements.txt >> "$LOG" 2>&1; then
                    log "OK   $repo — deps updated"
                else
                    log "FAIL $repo — pip install failed"
                    PULL_FAILURES=$((PULL_FAILURES + 1))
                    FAILED_REPOS+=("$repo (pip)")
                fi
                rm -rf "$PIP_TMPDIR"
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
                # Gate it either way: a restart that errored is exactly the
                # case the T1-3 health gate below must fail loud on, not skip.
                RESTARTED_SERVICES+=("$unit")
            fi
        done
    fi
fi

# ── Sync metron-intraday from nousergon-data (config#1768 Phase 1) ─────────
# metron-intraday moved OFF ae-trading onto this box (2026-07-21):
# duplicated the intraday-price-alerts Lambda's work there, and ae-trading
# is off most of the day while ae-dashboard is always-on — this box is
# where daily-news already runs the same "always-on box picks up a
# nousergon-data-owned timer" pattern. Unit files stay canonical in
# nousergon-data's infrastructure/systemd/ (this box already clones
# nousergon-data as alpha-engine-data, see REPOS above) rather than being
# duplicated into this repo's own infrastructure/systemd/.
#
# Deliberately scoped to the two exact metron-intraday basenames, NOT a
# directory-wide glob — that source dir also ships daily-news.{service,
# timer} (already handled by this box's separate install-daily-news.sh +
# deploy-daily-news-units.yml merge-time SSM push path — mirroring that
# path here would double-install/-restart it via two independent
# mechanisms) and systemd-unit-drift-check.{service,timer} (already
# installed on this box by that same install-daily-news.sh, which copies
# both pairs — see its own comment). Only metron-intraday has no install
# path onto this box yet.
#
# Convergence via boot-pull (NOT a merge-time SSM push) is the deliberate
# choice here, matching existing precedent: deploy-daily-news-units.yml's
# own header explicitly contrasts the two mechanisms — daily-news gets a
# merge-time push because retrofitting one for metron-intraday was
# EXPLICITLY decided against ("relies on boot-pull self-healing ... since
# the trading box is off most of the day"). That reasoning flips in
# metron-intraday's favor now that its host is ae-dashboard: this box's
# boot-pull already runs on a bounded ≤24h daily timer (see file header),
# so next-boot-pull convergence has the same bounded-drift property a
# merge-time push would add, without a second deploy mechanism to maintain.
METRON_INTRADAY_SRC="/home/ec2-user/alpha-engine-data/infrastructure/systemd"
if [ -d "$METRON_INTRADAY_SRC" ]; then
    METRON_CHANGED=false
    for unit in metron-intraday.service metron-intraday.timer; do
        src="$METRON_INTRADAY_SRC/$unit"
        [ -f "$src" ] || continue
        target="/etc/systemd/system/$unit"
        if [ ! -f "$target" ]; then
            sudo cp "$src" "$target"
            log "SYNC $unit (new, src=$METRON_INTRADAY_SRC)"
            METRON_CHANGED=true
        elif ! diff -q "$src" "$target" >/dev/null 2>&1; then
            sudo cp "$src" "$target"
            log "SYNC $unit (updated, src=$METRON_INTRADAY_SRC)"
            METRON_CHANGED=true
        fi
    done
    if $METRON_CHANGED; then
        sudo systemctl daemon-reload
        log "systemctl daemon-reload (metron-intraday)"
    fi
    # Enable-reconcile every boot (not install-only), mirroring
    # ae-trading's sync_systemd_units_from() self-healing pattern
    # (config#2352 / the 2026-04-21 SNDK EOD incident class) rather than
    # this file's own simpler install-once loop above — this is a brand
    # new unit family for this box, so a manual `systemctl disable` or a
    # lost timers.target.wants/ symlink would otherwise never self-heal.
    if [ -f "$METRON_INTRADAY_SRC/metron-intraday.timer" ]; then
        if sudo systemctl enable --now metron-intraday.timer >> "$LOG" 2>&1; then
            log "OK   systemd: enable reconciled metron-intraday.timer"
        else
            log "WARN systemd: enable reconcile failed: metron-intraday.timer"
        fi
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
    RESTARTED_SERVICES+=("dashboard.service" "nous-ergon-live.service")
fi

# ── T1-3 post-restart health gate + auto-revert (config-I6742) ──────────────
# Every long-running service this run restarted must come back active.
# Previously a unit-sync restart could leave a service dead and boot-pull
# still exited 0 — the "user-facing service stays dead" outcome policy
# T1-2/T1-3 exists to prevent, invisible until the next box-health tick.
# Type=oneshot units are excluded: restarting one RUNS it, and is-active is
# not a health signal for a job.

is_gate_unit() {
    local unit_type
    unit_type=$(systemctl show -p Type --value "$1" 2>/dev/null)
    [ -n "$unit_type" ] && [ "$unit_type" != "oneshot" ]
}

wait_active() {
    local unit="$1" n=0
    while [ $n -lt 30 ]; do
        if systemctl is-active --quiet "$unit"; then
            return 0
        fi
        sleep 1
        n=$((n + 1))
    done
    return 1
}

FAILED_UNITS=""
if [ ${#RESTARTED_SERVICES[@]} -gt 0 ]; then
    for unit in $(printf '%s\n' "${RESTARTED_SERVICES[@]}" | sort -u); do
        is_gate_unit "$unit" || continue
        if wait_active "$unit"; then
            log "OK   health gate — $unit active"
        else
            log "FAIL health gate — $unit not active 30s after restart"
            FAILED_UNITS="$FAILED_UNITS $unit"
        fi
    done
fi

if [ -n "$FAILED_UNITS" ]; then
    log "REVERT health gate failed (${FAILED_UNITS# }) — rolling back this run's pulls"
    for i in "${!PULLED_REPOS[@]}"; do
        _repo="${PULLED_REPOS[$i]}"
        _prev="${PULLED_PREV[$i]}"
        case "$_prev" in
            ''|none|*[!0-9a-f]*)
                # Reverting to a guess is worse than not reverting (same rule
                # as deploy-on-merge.sh revert_to_last_good).
                log "WARN cannot revert $_repo — no usable previous sha ('$_prev')"
                continue ;;
        esac
        _revert_lock="$(git_sync_lock_path "$_repo")"
        if flock -w "$GIT_SYNC_LOCK_WAIT" "$_revert_lock" git -C "$_repo" reset --hard "$_prev" >> "$LOG" 2>&1; then
            log "REVERT $_repo -> $_prev"
        else
            log "REVERT FAILED $_repo — git reset to $_prev did not apply"
        fi
    done

    # Re-sync unit files from the reverted trees. A revert that leaves
    # new-sha units over old-sha code is a state neither sha was tested in —
    # the 2026-07-28 run-30404044358 failure mode deploy-on-merge.sh's
    # revert_to_last_good documents.
    for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
        [ -f "$unit" ] || continue
        name=$(basename "$unit")
        if [ -f "/etc/systemd/system/$name" ] && ! diff -q "$unit" "/etc/systemd/system/$name" >/dev/null 2>&1; then
            sudo cp "$unit" "/etc/systemd/system/$name"
            log "REVERT-SYNC $name"
        fi
    done
    for unit in metron-intraday.service metron-intraday.timer; do
        _src="$METRON_INTRADAY_SRC/$unit"
        [ -f "$_src" ] || continue
        if [ -f "/etc/systemd/system/$unit" ] && ! diff -q "$_src" "/etc/systemd/system/$unit" >/dev/null 2>&1; then
            sudo cp "$_src" "/etc/systemd/system/$unit"
            log "REVERT-SYNC $unit"
        fi
    done
    sudo systemctl daemon-reload

    STILL_FAILED=""
    for unit in $FAILED_UNITS; do
        sudo systemctl restart "$unit" 2>> "$LOG" || log "WARN post-revert restart $unit failed"
        if wait_active "$unit"; then
            log "OK   post-revert — $unit active"
        else
            STILL_FAILED="$STILL_FAILED $unit"
        fi
    done

    if [ -z "$STILL_FAILED" ]; then
        _gate_msg="auto-reverted to the previous SHAs and healthy again; the pulled commits are NOT live — investigate before they land via the next boot-pull"
    else
        _gate_msg="REVERT INCOMPLETE — still not active:${STILL_FAILED}. Manual intervention needed NOW"
    fi
    # ALERT_PY comes from alert_py.sh (config-I7168) — the declared krepis
    # venv, not whichever checkout happened to carry a krepis.
    :
    if [ -x "$ALERT_PY" ]; then
        "$ALERT_PY" -m krepis.alerts publish \
            --message "boot-pull health gate FAILED on $(hostname):${FAILED_UNITS} not active after restart — ${_gate_msg}. See /var/log/boot-pull.log." \
            --severity critical \
            --source boot-pull \
            --dedup-key "boot-pull-healthgate" \
            --dedup-window-min 1440 \
            || log "ALERT PUBLISH FAILED — boot-pull health-gate failure is UNREPORTED"
    else
        log "ALERT PUBLISH SKIPPED — $ALERT_PY missing; boot-pull health-gate failure is UNREPORTED"
    fi
fi

# Record where every repo now stands (post-pull, or post-revert). This is the
# T1-3 "sha recorded to a state file" half; the log has the narrative, this
# file has the machine-readable current set.
if mkdir -p "$(dirname "$LAST_PULL_SHAS")" 2>> "$LOG"; then
    for repo in "${REPOS[@]}"; do
        [ -d "$repo/.git" ] || continue
        printf '%s %s\n' "$repo" "$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)"
    done > "${LAST_PULL_SHAS}.tmp" && mv "${LAST_PULL_SHAS}.tmp" "$LAST_PULL_SHAS"
else
    log "WARN could not create $(dirname "$LAST_PULL_SHAS") — sha state file not written"
fi

if [ -n "$FAILED_UNITS" ]; then
    # systemd must see the gate failure regardless of whether the revert or
    # the alert worked — same contract as the PULL_FAILURES branch below.
    log "=== boot-pull FAILED health gate:${FAILED_UNITS} ==="
    exit 1
fi

# ── Report failures if any occurred ─────────────────────────────────────────
# The log file alone is not a signal — nobody reads /var/log/boot-pull.log
# until something else has already gone wrong.
#
# This used to construct flow-doctor by hand in a heredoc. It had been BROKEN
# since some earlier flow-doctor release and nobody knew, because the thing
# that was broken was the failure reporter itself (alpha-engine-config-I4509).
# Two independent faults, either one fatal:
#
#   1. `flow_doctor.init()` does not exist. flow-doctor 0.8.7 exports
#      FlowDoctor/FlowDoctorBuilder and no `init`; the call raised
#      AttributeError every time. The only trace was one stderr line,
#      `[boot-pull] flow-doctor report failed: module 'flow_doctor' has no
#      attribute 'init'`, inside a log nobody reads.
#   2. The env hydration was incomplete anyway. flow-doctor.yaml references
#      EIGHT ${VAR}s; the heredoc hydrated FOUR. Even with the API call fixed,
#      construction fails with ConfigError on TELEGRAM_BOT_TOKEN.
#
# Both faults come from the same root cause: this call site hand-rolled
# something the fleet already has a maintained interface for. `krepis.alerts`
# is the canonical alert CLI (config#1649) and is what box_health.sh on this
# same box already uses — verified reaching both SNS and Telegram. It resolves
# its own secrets, so there is no env-hydration list here to drift out of sync
# with flow-doctor.yaml.
if [ "$PULL_FAILURES" -gt 0 ]; then
    log "=== boot-pull completed with $PULL_FAILURES failure(s): ${FAILED_REPOS[*]} ==="

    # ALERT_PY comes from alert_py.sh (config-I7168) — the declared krepis
    # venv, not whichever checkout happened to carry a krepis.
    :
    if [ -x "$ALERT_PY" ]; then
        # Dedup on the failing repo set, not the message: the same repo failing
        # every boot should alert once a day, not once per boot. A NEW repo
        # failing changes the key and pages immediately.
        _dkey="boot-pull-$(printf '%s' "${FAILED_REPOS[*]}" | tr ' /' '__' | cut -c1-72)"
        "$ALERT_PY" -m krepis.alerts publish \
            --message "boot-pull FAILED on $(hostname): ${PULL_FAILURES} repo(s) could not be updated — ${FAILED_REPOS[*]}. The box may be running stale code. See /var/log/boot-pull.log." \
            --severity error \
            --source boot-pull \
            --dedup-key "$_dkey" \
            --dedup-window-min 1440 \
            || log "ALERT PUBLISH FAILED — boot-pull failure is UNREPORTED"
    else
        log "ALERT PUBLISH SKIPPED — $ALERT_PY missing; boot-pull failure is UNREPORTED"
    fi

    exit 1
fi

log "=== boot-pull completed successfully ==="
exit 0
