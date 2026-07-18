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
DASH_WEB_URL="http://localhost:3002/dash"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }
fail() { log "FAIL $*"; exit 1; }

# wait_for_health URL LABEL — poll a health endpoint for up to 30s. Defined
# early (moved up from §4) so the §0 Python-parity self-heal below can reuse
# it post-restart without duplicating the polling loop.
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

# paths_changed OLD_SHA NEW_SHA -- path [path...]
#
# Returns 0 (true) if `git diff OLD_SHA NEW_SHA -- paths...` shows any real
# change, and returns 1 (false) only when the diff genuinely has none.
#
# This deliberately does NOT use the `git diff ... | grep -q PATTERN`
# one-liner that every diff-gated block here used to use. That form is
# unsafe under `set -o pipefail` (which this script sets): GNU grep's `-q`
# exits as soon as it sees the first match, closing its end of the pipe.
# `git diff` writes its output to the pipe in ~4KB stdio-buffered chunks
# (confirmed via strace — NOT one write() per line, and NOT gated on the
# 64KB pipe capacity), so a diff spanning more than one chunk (e.g. the
# multi-path §2b-2e diffs, or any multi-file diff whose match falls in an
# early chunk) can have `git diff` still writing a *later* chunk after grep
# has already exited and closed the pipe. That write gets SIGPIPE, git diff
# exits 141, and under `pipefail` the whole pipeline reports 141 — even
# though grep DID find a real match. `if pipeline; then` then evaluates
# as false on a truthy diff. This was confirmed by direct reproduction:
# replaying the exact §2e box-health command 100x under `set -o pipefail`
# returned non-zero ~99/100 times despite the diff always containing real
# changes (config#2242).
#
# The fix: capture git diff's own output AND exit code first (no pipe to
# grep at all), so grep never has a chance to race git's writes. A git-diff
# failure (non-zero exit, e.g. bad revision/object-availability issue) is
# NOT silently treated as "no change" — it's logged loudly and treated as
# "assume changed", since re-running an idempotent installer unnecessarily
# is far cheaper than silently skipping a needed reinstall.
#
# NOTE (config#2338): paths_changed() itself is fine, but every call site
# below used to diff `${CURRENT_SHA}~1..${CURRENT_SHA}` — i.e. "what changed
# in THIS merge's single commit step". If a deploy never executes (SSM
# delivery failure, config#2227 signature — detected in deploy.yml but not
# healed), the *next* deploy's `~1..HEAD` window still only covers its own
# single step and permanently skips whatever the missed commit touched. The
# §3b-3d unit blocks below never had this problem because they don't diff
# commit ranges at all — they `cmp` the repo file directly against the live
# installed copy, so it doesn't matter how many deploys were missed; the gate
# just asks "does live state match repo state right now". file_state_stale()
# generalizes that same pattern for the requirements/nginx/installer gates
# that used to be commit-range-gated.
paths_changed() {
    local old_sha="$1" new_sha="$2"
    shift 2
    local diff_out
    local diff_rc
    diff_out=$(sudo -u ec2-user git diff "$old_sha" "$new_sha" -- "$@" 2>&1)
    diff_rc=$?
    if [ $diff_rc -ne 0 ]; then
        log "WARN git diff $old_sha $new_sha -- $* failed (exit $diff_rc) — assuming changed: $diff_out"
        return 0
    fi
    printf '%s\n' "$diff_out" | grep -q '^[+-]'
}

# file_state_stale DST SRC [SRC...]
#
# State-compare gate (config#2338): returns 0 (true — "stale, needs action")
# if DST is missing, or if DST's content doesn't match SRC (first arg after
# DST). Additional SRC args are only used for existence-checking (see
# any_src_missing below) — deploy-on-merge always compares content 1:1 for
# scripts/units, one file at a time, so callers loop over file pairs rather
# than passing multiple SRCs to a single call. Same `cmp -s` primitive the
# §3b-3d unit blocks already use, factored out so the requirements/nginx/
# installer gates below can use it too. This is self-healing by construction:
# it never looks at *how many* commits were skipped, only whether the box's
# current state matches the repo's current state.
file_state_stale() {
    local dst="$1" src="$2"
    [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"
}

# any_file_state_stale SRC:DST [SRC:DST...]
#
# Returns 0 (true) if ANY of the given "src:dst" pairs is stale per
# file_state_stale. Used by the §2b-2e installer gates, which each manage
# several files (a script + N systemd units) — if any one of them drifts
# from the repo, the whole idempotent installer re-runs (matching what the
# installer itself does: it re-copies everything unconditionally once
# invoked).
any_file_state_stale() {
    local pair src dst
    for pair in "$@"; do
        src="${pair%%:*}"
        dst="${pair#*:}"
        if file_state_stale "$dst" "$src"; then
            return 0
        fi
    done
    return 1
}

log "=== deploy-on-merge started — target=$TARGET_SHA ==="

cd "$REPO_DIR" || fail "cd $REPO_DIR"
CURRENT_SHA=$(sudo -u ec2-user git rev-parse HEAD)
log "repo HEAD -> $CURRENT_SHA"
log "$(sudo -u ec2-user git log --oneline -1)"

# ── 0. Python-version SSoT self-heal (config#2791) ──────────────────────────
# .python-version is the single declared Python version this repo builds
# against (CI's `test` job now reads the same file — mirrors the dash-web
# Node/.nvmrc parity guard, config#2711/PR#450). Unlike the Node guard (which
# only FAILS on mismatch), this is a self-heal: an interpreter drift on this
# box is fixable in-place (install the missing minor via dnf, rebuild the
# venv, swap it in atomically) rather than requiring a human to SSH in.
# Idempotent: no-ops once the box venv already matches the SSoT.
PYVER_SSOT_FILE="$REPO_DIR/.python-version"
if [ -f "$PYVER_SSOT_FILE" ] && [ -f "$REPO_DIR/.venv/bin/python" ]; then
    SSOT_PYVER="$(tr -d '[:space:]' < "$PYVER_SSOT_FILE")"
    SSOT_PYVER_MAJOR_MINOR="$(printf '%s' "$SSOT_PYVER" | grep -oE '^[0-9]+\.[0-9]+')"
    BOX_VENV_PYVER="$(sudo -u ec2-user "$REPO_DIR/.venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    if [ -z "$SSOT_PYVER_MAJOR_MINOR" ]; then
        log "WARN Python-parity self-heal: could not parse major.minor from $PYVER_SSOT_FILE (got '$SSOT_PYVER') — skipping"
    elif [ -z "$BOX_VENV_PYVER" ]; then
        log "WARN Python-parity self-heal: could not determine box venv's Python version — skipping"
    elif [ "$BOX_VENV_PYVER" = "$SSOT_PYVER_MAJOR_MINOR" ]; then
        log "OK   Python-parity self-heal: box venv already on $BOX_VENV_PYVER, matches SSoT $SSOT_PYVER_MAJOR_MINOR — no-op"
    else
        log "Python-parity self-heal: box venv is $BOX_VENV_PYVER, SSoT ($PYVER_SSOT_FILE) requires $SSOT_PYVER_MAJOR_MINOR — rebuilding venv"

        # 1. Install the target interpreter via dnf if not already present
        # (AL2023 amazonlinux repo carries pythonX.Y packages directly).
        PY_DNF_PKG="python${SSOT_PYVER_MAJOR_MINOR}"
        if ! command -v "/usr/bin/${PY_DNF_PKG}" >/dev/null 2>&1; then
            log "installing $PY_DNF_PKG via dnf"
            dnf install -y "${PY_DNF_PKG}" >>"$LOG" 2>&1 \
                || fail "dnf install $PY_DNF_PKG"
        fi
        NEW_PY_BIN="/usr/bin/${PY_DNF_PKG}"
        [ -x "$NEW_PY_BIN" ] || fail "Python-parity self-heal: $NEW_PY_BIN not found after dnf install"

        # 2. Stop the 4 Python-venv-backed services BEFORE touching .venv
        # (config#2835 postmortem: a venv's console-script shebangs are
        # ABSOLUTE paths baked in by `venv`/`pip install` at the path the
        # venv was BUILT at. The old flow built the venv at a staging path
        # then `mv`'d it into place — every shebang (streamlit, uvicorn,
        # pip, ...) kept pointing at the now-deleted staging path, so systemd
        # exec failed with a misleading ENOENT. The fix: build+install the
        # venv directly at its FINAL path ($REPO_DIR/.venv, never relocated
        # after install) so no shebang is ever wrong. That requires the venv
        # to not exist at the final path while we build, hence stopping
        # services first — this is now brief PLANNED downtime, not a live
        # swap out from under running processes.)
        systemctl stop dashboard nous-ergon-live crucible-dash crucible-dash-api 2>>"$LOG" \
            || fail "python-parity self-heal: stop services before venv rebuild"
        log "stopped dashboard, nous-ergon-live, crucible-dash, crucible-dash-api for venv rebuild"

        # 3. Preserve the old venv for rollback, then build+install the new
        # venv DIRECTLY at the final $REPO_DIR/.venv path (no relocation).
        OLD_VENV_BACKUP="$REPO_DIR/.venv-prev-$(date +%s)"
        mv "$REPO_DIR/.venv" "$OLD_VENV_BACKUP" || fail "python-parity self-heal: mv old .venv aside"

        # _rollback_venv: restore the preserved old venv and restart services
        # on it. Called on ANY failure from here on — a failed swap must
        # never leave the box crash-looping on a broken/partial venv
        # (config#2835 defect 2: the old flow's post-swap health-gate
        # failure called `fail` directly, WITHOUT restoring the preserved
        # old venv, leaving all 4 services crash-looping for ~25 minutes).
        _rollback_venv() {
            log "ROLLBACK python-parity self-heal: restoring $OLD_VENV_BACKUP -> $REPO_DIR/.venv"
            rm -rf "$REPO_DIR/.venv"
            if ! mv "$OLD_VENV_BACKUP" "$REPO_DIR/.venv"; then
                log "FAIL python-parity self-heal: rollback mv itself failed — box has NO venv at $REPO_DIR/.venv, manual intervention required NOW"
                return 1
            fi
            systemctl restart dashboard nous-ergon-live crucible-dash crucible-dash-api 2>>"$LOG"
            wait_for_health "$CONSOLE_URL" "dashboard (console) [post-rollback]" \
                && wait_for_health "$LIVE_URL" "nous-ergon-live [post-rollback]" \
                && wait_for_health "$DASH_URL" "crucible-dash [post-rollback]" \
                && wait_for_health "$DASH_API_URL" "crucible-dash-api [post-rollback]"
        }

        sudo -u ec2-user "$NEW_PY_BIN" -m venv "$REPO_DIR/.venv" >>"$LOG" 2>&1 \
            || { _rollback_venv; fail "python-parity self-heal: venv create at $REPO_DIR/.venv (rolled back to previous venv)"; }
        sudo -u ec2-user "$REPO_DIR/.venv/bin/python" -m pip install --upgrade pip >>"$LOG" 2>&1 \
            || { _rollback_venv; fail "python-parity self-heal: pip upgrade in new venv (rolled back to previous venv)"; }

        NEW_VENV_PIP_TMPDIR="$REPO_DIR/.pip-tmp-parity-heal"
        rm -rf "$NEW_VENV_PIP_TMPDIR"
        sudo -u ec2-user mkdir -p "$NEW_VENV_PIP_TMPDIR" \
            || { _rollback_venv; fail "python-parity self-heal: mkdir $NEW_VENV_PIP_TMPDIR (rolled back to previous venv)"; }
        sudo -u ec2-user env TMPDIR="$NEW_VENV_PIP_TMPDIR" \
            "$REPO_DIR/.venv/bin/python" -m pip install -r "$REPO_DIR/requirements.txt" >>"$LOG" 2>&1 \
            || { _rollback_venv; fail "python-parity self-heal: pip install into new venv (rolled back to previous venv)"; }
        rm -rf "$NEW_VENV_PIP_TMPDIR"
        log "built venv directly at final path $REPO_DIR/.venv on $SSOT_PYVER_MAJOR_MINOR — no relocation, shebangs are correct by construction (old venv preserved at $OLD_VENV_BACKUP for rollback)"

        # 4. Restart the 4 Python-venv-backed services (dashboard,
        # nous-ergon-live, crucible-dash + its crucible-dash-api sibling;
        # crucible-dash-web is Node/Next.js, not Python — untouched here).
        systemctl restart dashboard 2>>"$LOG" || { _rollback_venv; fail "python-parity self-heal: restart dashboard (rolled back to previous venv)"; }
        systemctl restart nous-ergon-live 2>>"$LOG" || { _rollback_venv; fail "python-parity self-heal: restart nous-ergon-live (rolled back to previous venv)"; }
        systemctl restart crucible-dash 2>>"$LOG" || { _rollback_venv; fail "python-parity self-heal: restart crucible-dash (rolled back to previous venv)"; }
        systemctl restart crucible-dash-api 2>>"$LOG" || { _rollback_venv; fail "python-parity self-heal: restart crucible-dash-api (rolled back to previous venv)"; }
        log "restarted dashboard, nous-ergon-live, crucible-dash, crucible-dash-api on new venv"

        # 5. Reuse the script's existing health-gate (§4) immediately, so a
        # bad interpreter swap is caught and visible NOW rather than only at
        # the end of the script after other work has also run. On failure,
        # AUTO-ROLLBACK (defect 2) instead of leaving the broken venv live.
        if ! wait_for_health "$CONSOLE_URL" "dashboard (console) [python-parity swap]" \
            || ! wait_for_health "$LIVE_URL" "nous-ergon-live [python-parity swap]" \
            || ! wait_for_health "$DASH_URL" "crucible-dash [python-parity swap]" \
            || ! wait_for_health "$DASH_API_URL" "crucible-dash-api [python-parity swap]"; then
            if _rollback_venv; then
                fail "python-parity self-heal: post-swap health gate failed on $SSOT_PYVER_MAJOR_MINOR venv — ROLLED BACK to previous venv successfully, all 4 services healthy again on old venv"
            else
                fail "python-parity self-heal: post-swap health gate failed on $SSOT_PYVER_MAJOR_MINOR venv AND rollback also failed — box may have NO working venv, manual intervention required NOW"
            fi
        fi
        log "OK   Python-parity self-heal: all 4 services healthy on $SSOT_PYVER_MAJOR_MINOR"

        # requirements.txt is already installed fresh into the new venv
        # above (it must be, to build a working venv at all) — skip §1's
        # state-compare-gated pip install this run so we don't redundantly
        # reinstall into the venv we just built, and record the stamp §1
        # compares against (same path as REQUIREMENTS_STAMP below) so the
        # next deploy's state-compare reflects reality.
        mkdir -p /etc/dashboard-deploy || fail "python-parity self-heal: mkdir stamp dir"
        cp "$REPO_DIR/requirements.txt" /etc/dashboard-deploy/requirements.txt.installed \
            || fail "python-parity self-heal: update requirements stamp"
        SKIP_REQUIREMENTS_INSTALL=1
    fi
fi

# ── 1. Refresh deps (as the owning user) ────────────────────────────────────
# State-compare (config#2338), not commit-range diff: a stamp file records
# the requirements.txt content that was installed last time this block ran.
# If a deploy is skipped/fails, the stamp simply stays stale — the NEXT
# deploy compares the repo's current requirements.txt against the stamp
# (not against `HEAD~1`), so it doesn't matter how many commits were missed
# in between. Steady-state (stamp already matches repo) stays a single
# `cmp -s` — no pip invocation at all.
REQUIREMENTS_STAMP="/etc/dashboard-deploy/requirements.txt.installed"
# Gate on the venv INTERPRETER being executable, not on the presence of the
# `.venv/bin/pip` console-script wrapper. That wrapper carries an absolute-path
# `#!` shebang baked in by `venv`/`pip` at build time (config#2835 class); if
# the venv was ever relocated or the wrapper otherwise went stale, the file
# still EXISTS (so a `-f` test passes) but `env` fails to execve it with
# `env: '.venv/bin/pip': No such file or directory` (rc=127) — the exact
# 2026-07-18 Deploy false-red (run 29654297139, config#2938 krepis bump). The
# pip MODULE in site-packages is unaffected, so we invoke pip as
# `.venv/bin/python -m pip` below (shebang-immune) and only need the
# interpreter itself to be runnable here.
if [ "${SKIP_REQUIREMENTS_INSTALL:-0}" != "1" ] && [ -x ".venv/bin/python" ] && [ -f "requirements.txt" ]; then
    if file_state_stale "$REQUIREMENTS_STAMP" "requirements.txt"; then
        log "requirements.txt differs from last-installed stamp — pip install"
        # requirements.txt is a uv-compiled lockfile. It is compiled in CI
        # (GH runner, currently Python 3.12) but INSTALLED here against the
        # box venv — if the two interpreters differ, a version that resolves
        # cleanly for the compiler can be un-installable here (Requires-Python
        # >=3.11 pins like numpy 2.4.6 / pandas 3.0.3 ship no <3.11 wheel).
        # Log the venv interpreter up front so a mismatch is visible in the
        # SSM/CI output rather than inferred (2026-07-17 numpy/pandas parity
        # incident: config#1592-sibling — a hand-patched numpy pin masked that
        # pandas 3.0.3 carries the same >=3.11 floor; the real failure lived
        # only in $LOG on the box, invisible to CI/ci-watch).
        log "venv python -> $(sudo -u ec2-user .venv/bin/python --version 2>&1)"

        # TMPDIR fix (config#2792/#2736, 2026-07-17): pip has NO dedicated
        # option for its build/download scratch space — it always uses
        # tempfile.gettempdir(), i.e. $TMPDIR or /tmp. On this box, /tmp is a
        # 957MB tmpfs (small, RAM-backed, and shared with unrelated
        # box-resident state — morning-signal scratch files, research.db,
        # etc.), while / has 16G+ free. A full-closure install of this lock
        # (pandas/pyarrow/numpy/pillow/streamlit all downloading+building
        # concurrently) can overflow that 957M tmpfs and fail with
        # `OSError: [Errno 28] No space left on device` even though the real
        # root disk never gets close to full. This was mistaken for "box pip
        # 22.3.1 / Python 3.11 can't install the new uv lockfile" (config#2736)
        # — live SSM reproduction on i-09b539c844515d549 disproved that: the
        # IDENTICAL install, with pip 22.3.1 on Python 3.11.14 unchanged,
        # succeeds cleanly once TMPDIR points at a directory on the root
        # filesystem instead of the tmpfs. Point pip's scratch dir at the
        # root filesystem so its space use is bounded by the 16G+ free there,
        # not the 957M tmpfs.
        PIP_TMPDIR="$REPO_DIR/.pip-tmp"
        rm -rf "$PIP_TMPDIR"
        sudo -u ec2-user mkdir -p "$PIP_TMPDIR" || fail "mkdir pip tmpdir $PIP_TMPDIR"

        # Pre-pip fail-loud disk guards. Two distinct, both-real risk classes:
        #   (a) the CURRENT failure mode — TMPDIR's own filesystem (now the
        #       root fs via PIP_TMPDIR above) fills. Checked against
        #       PIP_TMPDIR specifically (not assumed to be "/") so this stays
        #       correct if TMPDIR is ever repointed elsewhere.
        #   (b) the OLD/dormant failure mode — root "/" itself fills
        #       (config#2227 class). Still checked directly since PIP_TMPDIR
        #       living on "/" today doesn't guarantee it always will.
        # Threshold 90% mirrors config#2227's disk-alarm threshold. Message
        # is self-classifying from SSM stdout alone (config#2792) — no
        # console-spelunking needed to tell "disk full" from "resolver error".
        for guard_path in "$PIP_TMPDIR" "/"; do
            guard_pcent="$(df --output=pcent "$guard_path" 2>/dev/null | tail -1 | tr -dc '0-9')"
            guard_mount="$(df --output=target "$guard_path" 2>/dev/null | tail -1 | tr -d ' ')"
            if [ -n "$guard_pcent" ] && [ "$guard_pcent" -ge 90 ]; then
                fail "DISK FULL — ${guard_pcent}% used on ${guard_mount:-$guard_path}, deploy aborted before pip (config#2227/#2792/#2736 class; a rerun cannot heal this without freeing space)"
            fi
        done

        # Capture pip's combined output (drop --quiet: we KEEP the detail),
        # tee the tail to stdout on failure so the REAL pip error reaches SSM
        # StandardOutputContent (and thus CI + ci-watch), never buried in $LOG.
        # Fail-loud: a real failure is made LOUDER here, never quieter.
        PIP_OUT=$(sudo -u ec2-user env TMPDIR="$PIP_TMPDIR" .venv/bin/python -m pip install -r requirements.txt 2>&1)
        PIP_RC=$?
        printf '%s\n' "$PIP_OUT" >>"$LOG"
        rm -rf "$PIP_TMPDIR"
        if [ $PIP_RC -ne 0 ]; then
            log "pip install FAILED (rc=$PIP_RC) — last 40 lines of pip output:"
            printf '%s\n' "$PIP_OUT" | tail -40
            fail "pip install requirements.txt"
        fi
        # Record what was just installed so the next run's state-compare
        # gate (config#2338) is against reality, not a commit range.
        mkdir -p "$(dirname "$REQUIREMENTS_STAMP")" || fail "mkdir requirements stamp dir"
        cp requirements.txt "$REQUIREMENTS_STAMP" || fail "update requirements.txt stamp"
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
# State-compare (config#2338), not commit-range diff: the live file at
# NGINX_CONF_LIVE IS the "installed copy" (same role as the systemd unit
# DSTs in §3b-3d below), so we cmp the repo copy directly against it instead
# of diffing `HEAD~1..HEAD`. A missed deploy just leaves the live file stale
# by however many commits — this gate doesn't care, it only asks whether the
# live file matches the repo file right now.
# nginx.conf is the source of truth for the routing layer (server_name +
# proxy_pass + sub_filter rules); previously a config edit required
# manually SSH'ing in to copy + reload. Now the fast-path auto-applies it.
#
# Order matters: nginx step runs BEFORE the streamlit restarts so a
# broken nginx config fails the deploy without bouncing streamlit.
NGINX_CONF_REPO="$REPO_DIR/infrastructure/nginx.conf"
NGINX_CONF_LIVE="/etc/nginx/conf.d/nousergon.conf"
if [ -f "$NGINX_CONF_REPO" ]; then
    if file_state_stale "$NGINX_CONF_LIVE" "$NGINX_CONF_REPO"; then
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
# unpaged (morning-signal#77).
#
# State-compare (config#2338), not commit-range diff: compare each
# repo-tracked source file directly against its installed on-box copy (same
# src:dst pairs install-morning-signal-watchdog.sh itself copies) instead of
# diffing `HEAD~1..HEAD`. A missed deploy leaves the installed copies stale
# by however many commits — irrelevant here, since the gate only checks
# "does the box currently match the repo", same as §3b-3d below.
WATCHDOG_INFRA="$REPO_DIR/infrastructure"
if any_file_state_stale \
    "$WATCHDOG_INFRA/morning-signal-watchdog.sh:/usr/local/bin/morning-signal-watchdog.sh" \
    "$WATCHDOG_INFRA/systemd/morning-signal-watchdog.service:/etc/systemd/system/morning-signal-watchdog.service" \
    "$WATCHDOG_INFRA/systemd/morning-signal-watchdog.timer:/etc/systemd/system/morning-signal-watchdog.timer"; then
    log "morning-signal watchdog wrapper/units differ from installed copies — re-installing"
    bash "$REPO_DIR/infrastructure/install-morning-signal-watchdog.sh" >>"$LOG" 2>&1 \
        || fail "install-morning-signal-watchdog.sh"
    log "re-installed morning-signal watchdog"
fi

# ── 2c. Re-install morning-signal core units if they changed ───────────────
# The morning-signal service/timer/drop-ins + the generate-only recovery
# wrapper are box-provisioned out of the repo tree by install-morning-signal.sh
# (they used to be unmanaged box-only units — morning-signal#79).
#
# State-compare (config#2338): same src:dst pairs install-morning-signal.sh
# copies, checked directly against the box instead of via `HEAD~1..HEAD`.
MS_INFRA="$REPO_DIR/infrastructure"
if any_file_state_stale \
    "$MS_INFRA/systemd/morning-signal.service:/etc/systemd/system/morning-signal.service" \
    "$MS_INFRA/systemd/morning-signal.timer:/etc/systemd/system/morning-signal.timer" \
    "$MS_INFRA/systemd/morning-signal.service.d/10-after-news.conf:/etc/systemd/system/morning-signal.service.d/10-after-news.conf" \
    "$MS_INFRA/systemd/morning-signal.service.d/10-memory.conf:/etc/systemd/system/morning-signal.service.d/10-memory.conf" \
    "$MS_INFRA/morning-signal-recover.sh:/usr/local/bin/morning-signal-recover.sh"; then
    log "morning-signal core units/recovery wrapper differ from installed copies — re-installing"
    bash "$REPO_DIR/infrastructure/install-morning-signal.sh" >>"$LOG" 2>&1 \
        || fail "install-morning-signal.sh"
    log "re-installed morning-signal core units"
fi

# ── 2d. Re-install morning-signal OSS bakeoff units if they changed ────────
# The weekly Phase B shadow-bakeoff (config#1659) timer/service.
#
# State-compare (config#2338): a not-yet-installed dst (first rollout) is
# "stale" by definition (file_state_stale treats a missing dst as stale),
# so this still self-provisions on first introduction with no manual step —
# same as the old diff-vs-parent gate did for brand-new files, but now also
# self-heals if a rollout deploy was missed entirely.
BAKEOFF_INFRA="$REPO_DIR/infrastructure"
if any_file_state_stale \
    "$BAKEOFF_INFRA/systemd/morning-signal-bakeoff.service:/etc/systemd/system/morning-signal-bakeoff.service" \
    "$BAKEOFF_INFRA/systemd/morning-signal-bakeoff.timer:/etc/systemd/system/morning-signal-bakeoff.timer"; then
    log "morning-signal bakeoff units differ from installed copies — re-installing"
    bash "$REPO_DIR/infrastructure/install-morning-signal-bakeoff.sh" >>"$LOG" 2>&1 \
        || fail "install-morning-signal-bakeoff.sh"
    log "re-installed morning-signal bakeoff units"
fi

# ── 2e. Re-install box-health/hygiene watchdog if its script/units changed ──
# box_health.sh + box_hygiene.sh + their units + the journald size cap are
# /usr/local/bin + /etc provisioned OUT of the repo tree by
# install-box-health.sh (config#2227).
#
# State-compare (config#2338): same src:dst pairs install-box-health.sh
# copies (including the journald drop-in, which install-box-health.sh itself
# already state-compares internally before restarting journald).
BOX_HEALTH_INFRA="$REPO_DIR/infrastructure"
if any_file_state_stale \
    "$BOX_HEALTH_INFRA/box_health.sh:/usr/local/bin/box_health.sh" \
    "$BOX_HEALTH_INFRA/box_hygiene.sh:/usr/local/bin/box_hygiene.sh" \
    "$BOX_HEALTH_INFRA/systemd/box-health.service:/etc/systemd/system/box-health.service" \
    "$BOX_HEALTH_INFRA/systemd/box-health.timer:/etc/systemd/system/box-health.timer" \
    "$BOX_HEALTH_INFRA/systemd/box-hygiene.service:/etc/systemd/system/box-hygiene.service" \
    "$BOX_HEALTH_INFRA/systemd/box-hygiene.timer:/etc/systemd/system/box-hygiene.timer" \
    "$BOX_HEALTH_INFRA/systemd/journald-size-cap.conf:/etc/systemd/journald.conf.d/size-cap.conf"; then
    log "box-health/hygiene script or units differ from installed copies — re-installing"
    bash "$REPO_DIR/infrastructure/install-box-health.sh" >>"$LOG" 2>&1 \
        || fail "install-box-health.sh"
    log "re-installed box-health/hygiene"
fi

# ── 3. Self-provision dashboard and nous-ergon-live unit files ──────────────
# Both services ship in the repo; install/refresh them on unit-file diff
# (or first deploy) so they can never drift from the repo copy — same
# idempotent pattern as crucible-dash/dash-api/dash-web (§3b-3d).
DASHBOARD_UNIT_SRC="$REPO_DIR/infrastructure/dashboard.service"
DASHBOARD_UNIT_DST="/etc/systemd/system/dashboard.service"
if [ ! -f "$DASHBOARD_UNIT_DST" ] || ! cmp -s "$DASHBOARD_UNIT_SRC" "$DASHBOARD_UNIT_DST"; then
    cp "$DASHBOARD_UNIT_SRC" "$DASHBOARD_UNIT_DST" 2>>"$LOG" || fail "install dashboard unit"
    systemctl daemon-reload 2>>"$LOG" || fail "daemon-reload for dashboard"
    systemctl enable dashboard 2>>"$LOG" || fail "enable dashboard"
    log "installed/refreshed dashboard.service unit"
fi

LIVE_UNIT_SRC="$REPO_DIR/live/infrastructure/nous-ergon-live.service"
LIVE_UNIT_DST="/etc/systemd/system/nous-ergon-live.service"
if [ ! -f "$LIVE_UNIT_DST" ] || ! cmp -s "$LIVE_UNIT_SRC" "$LIVE_UNIT_DST"; then
    cp "$LIVE_UNIT_SRC" "$LIVE_UNIT_DST" 2>>"$LOG" || fail "install nous-ergon-live unit"
    systemctl daemon-reload 2>>"$LOG" || fail "daemon-reload for nous-ergon-live"
    systemctl enable nous-ergon-live 2>>"$LOG" || fail "enable nous-ergon-live"
    log "installed/refreshed nous-ergon-live.service unit"
fi

# ── 3a. Restart both streamlit services (we are root) ──────────────────────
# Both services run from this same repo. Two-second stagger avoids a
# simultaneous blip on console + live site.
systemctl restart dashboard 2>>"$LOG" || fail "restart dashboard"
log "restarted dashboard.service"
sleep 2
systemctl restart nous-ergon-live 2>>"$LOG" || fail "restart nous-ergon-live"
log "restarted nous-ergon-live.service"

# ── 3b. Crucible /dash service (config#1957) — idempotent self-provision ────
# Same pattern as the dashboard/nous-ergon-live units above (§3).
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

# ── 3d. Crucible dash-web (Next.js, config#1973 9-C) ───────────────────────
# Build only when dash-web/ changed (or no build exists yet) — npm ci +
# next build are the expensive steps; unit self-provision mirrors 3b/3c.
WEB_DIR="$REPO_DIR/dash-web"
if [ -d "$WEB_DIR" ]; then
    # Node-parity guard (config#2711): CI builds dash-web against the major
    # pinned in dash-web/.nvmrc, but this box's `npm`/`node` on PATH was
    # never tied to that same source of truth — a CI-green major (e.g. next
    # 15->16, which needs Node >=20.9) can still silently fail or misbuild
    # here if the box runs a different major. Fail loud BEFORE the build so
    # a mismatch surfaces as a clear deploy error instead of a silent or
    # confusing on-box build failure.
    if [ -f "$WEB_DIR/.nvmrc" ]; then
        REQUIRED_NODE_MAJOR="$(tr -dc '0-9' < "$WEB_DIR/.nvmrc")"
        BOX_NODE_VERSION="$(sudo -u ec2-user node --version 2>/dev/null || true)"
        BOX_NODE_MAJOR="$(printf '%s' "$BOX_NODE_VERSION" | sed -n 's/^v\([0-9]\+\)\..*/\1/p')"
        if [ -z "$BOX_NODE_MAJOR" ]; then
            fail "dash-web Node-parity guard: could not determine box node --version (got '$BOX_NODE_VERSION') — install Node or fix PATH for ec2-user"
        elif [ "$BOX_NODE_MAJOR" != "$REQUIRED_NODE_MAJOR" ]; then
            fail "dash-web Node-parity guard: box runs node $BOX_NODE_VERSION (major $BOX_NODE_MAJOR) but dash-web/.nvmrc requires major $REQUIRED_NODE_MAJOR — upgrade the box's Node to $REQUIRED_NODE_MAJOR.x, or if the box is intentionally staying put, edit dash-web/.nvmrc (and CI's setup-node step, which reads the same file) to match the box's actual major"
        fi
    fi
    if paths_changed "${CURRENT_SHA}~1" "$CURRENT_SHA" dash-web/ || [ ! -d "$WEB_DIR/.next" ]; then
        log "dash-web changed (or unbuilt) — npm ci + next build"
        sudo -u ec2-user bash -c "cd '$WEB_DIR' && npm ci --no-audit --no-fund && npm run build" >>"$LOG" 2>&1             || fail "dash-web npm build"
        log "dash-web built"
    fi
    WEB_UNIT_SRC="$REPO_DIR/infrastructure/crucible-dash-web.service"
    WEB_UNIT_DST="/etc/systemd/system/crucible-dash-web.service"
    if [ ! -f "$WEB_UNIT_DST" ] || ! cmp -s "$WEB_UNIT_SRC" "$WEB_UNIT_DST"; then
        cp "$WEB_UNIT_SRC" "$WEB_UNIT_DST" 2>>"$LOG" || fail "install crucible-dash-web unit"
        systemctl daemon-reload 2>>"$LOG" || fail "daemon-reload for crucible-dash-web"
        systemctl enable crucible-dash-web 2>>"$LOG" || fail "enable crucible-dash-web"
        log "installed/refreshed crucible-dash-web.service unit"
    fi
    sleep 1
    systemctl restart crucible-dash-web 2>>"$LOG" || fail "restart crucible-dash-web"
    log "restarted crucible-dash-web.service"
fi

# ── 4. Health check ─────────────────────────────────────────────────────────
# Streamlit's /_stcore/health returns 200 OK with body "ok" once the
# server is ready. Give it up to 30s per service to bind the port.
# (wait_for_health itself is defined near the top of this script, above §0,
# so the Python-parity self-heal can reuse it post-restart.)

wait_for_health "$CONSOLE_URL" "dashboard (console)" || fail "console health"
wait_for_health "$LIVE_URL" "nous-ergon-live" || fail "live health"
wait_for_health "$DASH_URL" "crucible-dash" || fail "crucible-dash health"
wait_for_health "$DASH_API_URL" "crucible-dash-api" || fail "crucible-dash-api health"
if [ -d "$REPO_DIR/dash-web" ]; then
    wait_for_health "$DASH_WEB_URL" "crucible-dash-web" || fail "crucible-dash-web health"
fi

log "=== deploy-on-merge completed successfully — sha=$CURRENT_SHA ==="
exit 0
