#!/bin/bash
# install-resource-limits.sh — render and install systemd drop-ins from budget.yaml.
#
# The drop-ins are GENERATED, never hand-edited on the box. budget.yaml is the
# single source of truth; check_memory_budget.py --installed asserts the box
# matches it. Hand-editing a drop-in is drift, and the check will fail.
#
# Before this existed, the limits lived only on the box (alpha-engine-config
# -I4440): they were invisible to review, unreproducible on a rebuild, and
# silently lost on any instance replacement.
#
# Usage:
#   sudo ./install-resource-limits.sh                    # install + daemon-reload
#   sudo ./install-resource-limits.sh --dry-run          # print what would change
#   sudo ./install-resource-limits.sh --verify           # restart each unit + assert port (acceptance criteria I4512)
#   sudo ./install-resource-limits.sh --dry-run --verify # print what would restart
#
# --verify: after installing limits, restarts each limited service sequentially
# and asserts it binds its port within a timeout. Catches the defect that hit
# at the I4451 deploy: a ceiling that permits running but prevents booting.
# Without this, a unit whose cap is below its startup peak reports active while
# never binding its port -- the exact failure mode that went undetected on
# litellm-proxy and llm-egress-proxy (alpha-engine-config-I4512).
#
# Policy: nous-ergon-ops/policies/shared-application-host-policy.md T1-1, T1-2.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUDGET="${BUDGET:-$HERE/systemd/resource-limits/budget.yaml}"

# Where `systemctl set-property --runtime <unit> ...` writes overrides.
# Outranks the drop-in this script generates (see the block below) and is
# root-owned, cleared on reboot. Overridable via env var for the same reason
# BUDGET now is: it is the only way to exercise the guard below off-box, in
# CI, without a real systemd (alpha-engine-config-I6277).
RUNTIME_DROPIN_ROOT="${RUNTIME_DROPIN_ROOT:-/run/systemd/system.control}"

# 99- so this drop-in sorts LAST and therefore wins. systemd merges drop-ins in
# alphabetical order with later files overriding earlier ones, and the legacy
# hand-written files on this box are named `override.conf` and `10-memory.conf`.
# A `10-` prefix here would have been silently overridden by `override.conf` --
# the budget would have been installed and had no effect.
DROPIN_NAME="99-resource-limits.conf"

# Legacy hand-written memory drop-ins that this file supersedes. Every one was
# verified 2026-07-27 to contain ONLY [Service] + Memory{High,Max} — no
# ExecStart, Environment, or other settings — so removing them loses nothing.
# They are removed rather than left in place because dead config that no longer
# takes effect is exactly the drift this mechanism exists to eliminate.
# NOTE: morning-signal.service.d/10-memory.conf is deliberately NOT touched.
# It is a TIMER JOB: since 2026-07-29 it is declared in budget.yaml's
# `timer_jobs:` and IS checked (drift + the batch-headroom bound), but it is not
# RENDERED here — this script only writes drop-ins for `services:`, and a timer
# job's caps live with the unit that owns them. Checked, not rendered; the older
# note said "out of this budget's scope", which stopped being true when the
# declaration replaced the suppression.
LEGACY_NAMES=("override.conf" "10-memory.conf")

DRY_RUN=0
VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --verify) VERIFY=1 ;;
  esac
done

[[ -f "$BUDGET" ]] || { echo "missing $BUDGET" >&2; exit 1; }

PY=$(command -v python3)
"$PY" -c 'import yaml' 2>/dev/null || { echo "PyYAML required" >&2; exit 1; }

# Refuse to install a budget that does not fit. Installing an over-budget set
# is exactly the failure this whole mechanism exists to prevent, so the
# installer is the wrong place to be permissive.
if ! "$PY" "$HERE/check_memory_budget.py" --declared --quiet; then
    echo "REFUSING to install: declared budget exceeds the ceiling (see above)." >&2
    exit 1
fi

# ── Refuse to render a User= for an account that does not exist ─────────────
#
# On 2026-07-28 this installer wrote User=svc-<service> into all thirteen
# drop-ins from budget.yaml's new `user:` fields. The script that CREATES those
# accounts (create-service-users.sh) was never invoked — it is named create-*,
# and the routing guard that exists to catch exactly this globs install-*.sh,
# so it matched nothing. systemd cannot resolve a User= it cannot look up, so
# every unit that restarted died with 217/USER: five services down, and the
# other eight armed to die on their next restart.
#
# A generator that emits a reference to an account it neither creates nor
# verifies is fail-OPEN — it produces a unit file that is syntactically valid
# and unbootable. Checking is one getent per declared user, so the only reason
# not to check is that nobody thought of it. Fail closed, before ANY file is
# written: a deploy that stops loudly here is recoverable, a box whose fourteen
# units all reference missing accounts is not.
#
# This is deliberately a check on the RENDERED state, not a dependency on
# create-service-users.sh having run. It holds no matter how the accounts come
# to exist — by that script, by hand, or by a future provisioning path.
missing_users=$("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
print(" ".join(sorted({u for s in spec["services"] if (u := s.get("user", ""))})))
PYEOF
)
absent=""
for u in $missing_users; do
    getent passwd "$u" >/dev/null 2>&1 || absent="${absent} ${u}"
done
if [[ -n "${absent// /}" ]]; then
    echo "REFUSING to install: budget.yaml declares User= for account(s) that do not exist on this host:" >&2
    for u in $absent; do echo "    $u" >&2; done
    echo "Nothing was written. Create the accounts first (infrastructure/create-service-users.sh)," >&2
    echo "or remove the 'user:' field from those services in budget.yaml." >&2
    exit 1
fi

# ── Refuse to write a drop-in that a live --runtime override outranks ───────
#
# alpha-engine-config-I6277: `systemctl set-property --runtime <unit>
# MemoryMax=...` writes /run/systemd/system.control/<unit>.d/50-Memory*.conf,
# which OUTRANKS the /etc drop-in this script generates. `daemon-reload` does
# not touch /run, so writing the /etc file anyway produces config that has NO
# effect on the running unit: the operator's obvious next action after a
# MemoryMax drift breach silently does nothing, and the breach re-pages on
# the next tick. Measured live on metron-api.service, 2026-08-03 17:11-17:40
# UTC -- the override was still live and paging 30 minutes after this script
# ran.
#
# Checked as a PREFLIGHT over every declared unit, before ANY drop-in is
# written -- same shape as the missing-user check above, for the same
# reason: refusing partway through the loop would leave the box
# half-migrated (some units re-rendered, others not).
#
# Never auto-`systemctl revert` here. That would silently destroy an
# in-flight measurement -- config-I6263's un-censoring procedure is this
# exact mechanism used deliberately and temporarily.
runtime_override_hit=0
while IFS= read -r ro_unit; do
    [[ -z "$ro_unit" ]] && continue
    ro_dir="${RUNTIME_DROPIN_ROOT}/${ro_unit}.d"
    [[ -d "$ro_dir" ]] || continue
    for ro_conf in "$ro_dir"/*.conf; do
        [[ -f "$ro_conf" ]] || continue
        grep -qE '^(MemoryMax|MemoryHigh)=' "$ro_conf" || continue
        echo "REFUSING to write a drop-in for ${ro_unit}: a LIVE 'systemctl set-property --runtime' override is active at ${ro_conf}." >&2
        echo "  This outranks the /etc drop-in this script generates -- daemon-reload does NOT clear /run/systemd/system.control," >&2
        echo "  so writing anyway would produce a file with no effect on the running unit, and the drift breach re-pages next tick." >&2
        echo "  Revert the live override first:  systemctl revert ${ro_unit}" >&2
        echo "  Then re-run this installer." >&2
        runtime_override_hit=1
    done
done < <("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
for s in spec["services"]:
    print(s["unit"])
PYEOF
)
if [[ $runtime_override_hit -eq 1 ]]; then
    echo "Nothing was written." >&2
    exit 1
fi

CHANGED=0
while IFS='|' read -r unit user high max; do
    [[ -z "$unit" ]] && continue
    dir="/etc/systemd/system/${unit}.d"
    target="${dir}/${DROPIN_NAME}"

    # USER DIRECTIVE: set when budget.yaml declares a per-service user.
    # nginx already runs as its own user — it has NO user field and the
    # User= line is omitted (nginx owns its own service file's User=).
    user_directive=""
    if [[ -n "$user" ]]; then
        user_directive="User=${user}"
    fi

    # REMOVEIPC: safe only when the service has its own user. With a shared
    # identity (ec2-user), RemoveIPC=yes would destroy IPC objects belonging
    # to the other twelve services when this one stops. Per alpha-engine-
    # config-I4490 and the budget.yaml sandboxing section, this was
    # deliberately omitted until per-service users land.
    remove_ipc=""
    if [[ -n "$user" ]]; then
        remove_ipc="RemoveIPC=yes"
    fi

    rendered=$(cat <<EOF
# GENERATED by infrastructure/install-resource-limits.sh from
# infrastructure/systemd/resource-limits/budget.yaml — do not hand-edit.
# Hand edits are drift and will fail check_memory_budget.py --installed.
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
MemoryAccounting=yes
MemoryHigh=${high}
MemoryMax=${max}
Restart=always
RestartSec=5s
OOMPolicy=stop
${user_directive}
${remove_ipc}

# ── sandboxing (alpha-engine-config-I4490) ──────────────────────────────
# Thirteen services across eight products share the single ec2-user identity,
# so one compromised service reads what that identity can reach.
#
# MEASURED 2026-07-28, because the previous version of this comment overstated
# it and that overstatement was load-bearing — it was the stated justification
# for a per-service-user migration that took the box down (I4791/I155):
#   - the deploy PAT   READABLE by every service (owned by ec2-user, 0600)
#   - 16 git checkouts READABLE
#   - the two Cloudflare Origin private keys  NOT readable — they are
#     root-owned 0600 under /etc/ssl/private; nginx opens them as root before
#     dropping privileges. They were never exposed by the shared identity.
#
# So the real shared asset is one credential file, not a key vault. That is
# worth knowing before anyone re-litigates per-service users: the cheaper and
# strictly better control is to stop keeping a push-capable token on disk.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
EOF
)

    # Retire superseded legacy drop-ins for this unit first. Left in place they
    # are dead config (99- wins), which is drift waiting to confuse someone.
    for legacy in "${LEGACY_NAMES[@]}"; do
        lpath="${dir}/${legacy}"
        [[ -f "$lpath" ]] || continue
        if grep -qvE '^\s*($|#|\[Service\]|MemoryAccounting=|MemoryHigh=|MemoryMax=)' "$lpath"; then
            echo "SKIP removing $lpath — contains settings beyond memory; review by hand" >&2
            continue
        fi
        CHANGED=1
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "would remove superseded $lpath"
        else
            rm -f "$lpath"
            echo "removed superseded $lpath"
        fi
    done

    if [[ -f "$target" ]] && [[ "$(cat "$target")" == "$rendered" ]]; then
        continue
    fi

    CHANGED=1
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "would write $target (MemoryHigh=$high MemoryMax=$max)"
    else
        mkdir -p "$dir"
        printf '%s\n' "$rendered" > "$target"
        chmod 644 "$target"
        echo "wrote $target"
    fi
done < <("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
for s in spec["services"]:
    user = s.get("user", "")
    print(f"{s['unit']}|{user}|{s['memory_high']}|{s['memory_max']}")
PYEOF
)

if [[ $DRY_RUN -eq 1 ]]; then
    echo "(dry run — nothing written)"
    exit 0
fi

if [[ $CHANGED -eq 0 ]]; then
    echo "no changes; drop-ins already match budget.yaml"
    exit 0
fi

systemctl daemon-reload
echo "daemon-reload done."

# Regenerate the watchdog's service/port manifest from the same budget.yaml, so
# adding a service to the budget also puts it under box_health.sh coverage.
# Two lists is how the watchdog fell six services behind in the first place.
echo
"$PY" "$HERE/generate-box-manifest.py"
echo
echo "Drop-ins are installed but NOT yet applied to running processes."
echo "New MemoryMax takes effect on the running cgroup immediately;"
echo "Restart=/RestartSec= apply to the NEXT start of each unit."
echo
echo "Verify with: $HERE/check_memory_budget.py --installed"

# ── Restart smoke test (alpha-engine-config-I4512) ───────────────────────────
# A ceiling that permits running but prevents booting is the exact failure mode
# that went undetected on litellm-proxy and llm-egress-proxy.  --verify restarts
# each service and asserts it binds its port within RESTART_TIMEOUT seconds.
# This catches the defect at apply time, not at the next incident.
if [[ $VERIFY -eq 1 ]]; then
    echo
    echo "=== Restart smoke test (--verify) ==="
    RESTART_TIMEOUT=90  # seconds; llm-egress-proxy SSM calls can take >60s
    FAILED=0
    while IFS='|' read -r unit port; do
        [[ -z "$unit" ]] && continue
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "would restart $unit and wait for port $port"
            continue
        fi
        echo -n "  restarting $unit ... "
        if ! systemctl restart "$unit" 2>/dev/null; then
            echo "FAILED (systemctl restart exited non-zero)"
            FAILED=1
            continue
        fi
        # Poll for port binding with exponential backoff
        OK=0
        for wait in 2 4 8 15 30 31; do
            sleep "$wait"
            if ss -tln 2>/dev/null | grep -q ":${port} "; then
                OK=1
                break
            fi
            # Early-exit if the unit has already failed
            if ! systemctl is-active --quiet "$unit" 2>/dev/null; then
                echo "inactive after ${wait}s — service crashed"
                FAILED=1
                OK=2
                break
            fi
        done
        if [[ $OK -eq 1 ]]; then
            echo "OK (port $port)"
        elif [[ $OK -eq 0 ]]; then
            echo "TIMEOUT — port $port not bound after ${RESTART_TIMEOUT}s"
            FAILED=1
        fi
    done < <("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
for s in spec["services"]:
    port = s.get("port", "")
    print(f"{s['unit']}|{port}")
PYEOF
)
    if [[ $DRY_RUN -eq 0 ]]; then
        echo
        if [[ $FAILED -eq 0 ]]; then
            echo "All ${RESTART_TIMEOUT}s smoke tests passed."
        else
            echo "SOME SERVICES FAILED the restart smoke test — review output above." >&2
            exit 1
        fi
    fi
fi
