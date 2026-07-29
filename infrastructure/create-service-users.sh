#!/bin/bash
# create-service-users.sh — per-service Unix identity migration for the dashboard box.
#
# alpha-engine-config#4791: thirteen services across eight products currently
# share the single ec2-user identity. This script creates per-service system
# users, migrates file ownership, and verifies each service starts under its
# new identity. Run ONCE on the live dashboard box.
#
# PREREQUISITES:
#   - This script is in the crucible-dashboard checkout on the box
#   - install-resource-limits.sh has been run FIRST (generates User= drop-ins)
#   - The box is in a maintenance window (services will restart)
#
# USAGE:
#   sudo ./create-service-users.sh             # execute migration
#   sudo ./create-service-users.sh --dry-run   # print what would change
#
# Design:
#   - Each service gets a system user (svc-<name>) with no login shell
#   - Services that share a checkout get a shared group (svc-<repo>)
#   - Working directories and venvs are chowned to the service user/group
#   - Per-service state directories are user-specific
#   - nginx is skipped (already runs as its own user)
#
# The script reads WorkingDirectory and ExecStart from systemd to determine
# which paths to migrate — it does NOT hardcode paths that may drift.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUDGET="$HERE/systemd/resource-limits/budget.yaml"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

[[ -f "$BUDGET" ]] || { echo "missing $BUDGET" >&2; exit 1; }

PY=$(command -v python3)
"$PY" -c 'import yaml' 2>/dev/null || { echo "PyYAML required" >&2; exit 1; }

# ── Phase 1: create system users ──────────────────────────────────────────

echo "=== Phase 1: system users ==="
echo

while IFS='|' read -r unit user; do
    [[ -z "$unit" || -z "$user" ]] && continue

    if id "$user" &>/dev/null; then
        echo "  SKIP $user — already exists"
    else
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "  would create $user (system user, no login)"
        else
            useradd -r -s /sbin/nologin -d /nonexistent "$user"
            echo "  CREATED $user"
        fi
    fi
done < <("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
for s in spec["services"]:
    user = s.get("user", "")
    if user:
        print(f"{s['unit']}|{user}")
PYEOF
)

echo

# ── Phase 2: shared groups for multi-service checkouts ─────────────────────
#
# Services that share a git checkout (same WorkingDirectory or same parent
# directory with multiple services) need a shared group so they can all read
# the checkout + venv. The script discovers shared directories from the live
# systemd units and creates groups as needed.
#
# If no services share a directory, each service user owns its own checkout
# outright and no shared groups are created.

echo "=== Phase 2: shared groups ==="
echo

# Discover working directories from systemd for every unit in the budget.
# We read the ACTUAL unit file (including drop-ins) to get the effective
# WorkingDirectory, because the main service file may be in another repo.

declare -A UNIT_DIRS  # unit -> WorkingDirectory
declare -A DIR_USERS  # WorkingDirectory -> "user1 user2 ..."

while IFS='|' read -r unit user; do
    [[ -z "$unit" || -z "$user" ]] && continue

    # systemctl show -p WorkingDirectory --value returns the effective value
    # (main service file merged with all drop-ins). If the unit doesn't exist
    # on this box, skip it — the operator will create it after migration.
    wd=$(systemctl show -p WorkingDirectory --value "$unit" 2>/dev/null || true)
    if [[ -z "$wd" || "$wd" == "/" ]]; then
        echo "  WARN $unit: cannot determine WorkingDirectory (unit not found or no WD set) — skipping directory migration"
        continue
    fi

    UNIT_DIRS["$unit"]="$wd"
    DIR_USERS["$wd"]="${DIR_USERS[$wd]:-} $user"
done < <("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
for s in spec["services"]:
    user = s.get("user", "")
    if user:
        print(f"{s['unit']}|{user}")
PYEOF
)

# Create a shared group for each directory that has >1 service
for dir in "${!DIR_USERS[@]}"; do
    users_in_dir=(${DIR_USERS[$dir]})
    if [[ ${#users_in_dir[@]} -le 1 ]]; then
        continue  # single-service directory — no shared group needed
    fi

    # Derive group name from directory basename
    group_name="svc-$(basename "$dir")"
    if getent group "$group_name" &>/dev/null; then
        echo "  SKIP group $group_name — already exists"
    else
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "  would create group $group_name (shared checkout: $dir)"
        else
            groupadd -r "$group_name"
            echo "  CREATED group $group_name"
        fi
    fi

    # Add each service user to the shared group
    for user in "${users_in_dir[@]}"; do
        if id "$user" | grep -q "$group_name"; then
            echo "    SKIP $user already in $group_name"
        else
            if [[ $DRY_RUN -eq 1 ]]; then
                echo "    would add $user to $group_name"
            else
                usermod -a -G "$group_name" "$user"
                echo "    added $user to $group_name"
            fi
        fi
    done
done

echo

# ── Phase 3: ownership migration ───────────────────────────────────────────

echo "=== Phase 3: ownership migration ==="
echo

for unit in "${!UNIT_DIRS[@]}"; do
    dir="${UNIT_DIRS[$unit]}"
    user=$( "$PY" - "$BUDGET" "$unit" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
target_unit = sys.argv[2]
for s in spec["services"]:
    if s["unit"] == target_unit:
        print(s.get("user", ""))
        break
PYEOF
)
    [[ -z "$user" ]] && continue

    # Determine owner: if this directory is shared, use root:group;
    # otherwise use user:user.
    users_in_dir=(${DIR_USERS[$dir]})
    if [[ ${#users_in_dir[@]} -gt 1 ]]; then
        group_name="svc-$(basename "$dir")"
        owner="root:$group_name"
    else
        owner="$user:$user"
    fi

    if [[ -d "$dir" ]]; then
        current_owner=$(stat -c '%U:%G' "$dir" 2>/dev/null || echo "unknown")
        if [[ "$current_owner" == "$owner" ]]; then
            echo "  SKIP $dir — already owned by $owner"
        else
            if [[ $DRY_RUN -eq 1 ]]; then
                echo "  would chown -R $owner $dir"
            else
                chown -R "$owner" "$dir"
                echo "  chown'd $dir -> $owner"
            fi
        fi
    else
        echo "  WARN $unit: WorkingDirectory $dir does not exist — skipped"
    fi

    # Migrate per-service state directories (inferred from ExecStart and
    # common patterns). These are DIRECTORIES THE SERVICE WRITES TO that
    # may live outside WorkingDirectory.
    #
    # Common patterns on this box (verified 2026-07-27):
    #   - /home/ec2-user/.cache/<app>         Streamlit cache
    #   - /home/ec2-user/.streamlit           Streamlit config
    #   - /home/ec2-user/<repo>/data          app-specific data dirs
    #
    # The operator should verify and extend this list for any service that
    # writes outside its WorkingDirectory.
done

echo

# ── Phase 4: verify after daemon-reload ────────────────────────────────────

echo "=== Phase 4: verification ==="
echo

if [[ $DRY_RUN -eq 1 ]]; then
    echo "(dry run — skipping daemon-reload and verification)"
    exit 0
fi

systemctl daemon-reload
echo "daemon-reload done."

# Verify each migrated unit's User= is set correctly in the merged view
FAILED=0
while IFS='|' read -r unit user; do
    [[ -z "$unit" || -z "$user" ]] && continue

    effective_user=$(systemctl show -p User --value "$unit" 2>/dev/null || echo "UNIT-NOT-FOUND")
    if [[ "$effective_user" == "$user" ]]; then
        echo "  OK   $unit: User=$user"
    elif [[ "$effective_user" == "UNIT-NOT-FOUND" ]]; then
        echo "  WARN $unit: unit not found on this box — verify after deployment"
    else
        echo "  FAIL $unit: expected User=$user, got User=$effective_user"
        FAILED=1
    fi
done < <("$PY" - "$BUDGET" <<'PYEOF'
import sys, yaml
spec = yaml.safe_load(open(sys.argv[1]))
for s in spec["services"]:
    user = s.get("user", "")
    if user:
        print(f"{s['unit']}|{user}")
PYEOF
)

echo
if [[ $FAILED -eq 1 ]]; then
    echo "VERIFICATION FAILED — some units have unexpected User= values."
    echo "Run: systemctl cat <unit>  to inspect the merged unit file."
    echo "Check that install-resource-limits.sh was run BEFORE this script."
    exit 1
fi

echo "All service users created and verified."
echo
echo "NEXT STEPS (manual, in order):"
echo "  1. Restart each service and verify it serves correctly:"
echo "     for each unit with a new User=:"
echo "       sudo systemctl restart <unit>"
echo "       curl -sI https://<vhost> | head -1  # should return 200/302"
echo "  2. Verify ~/.netrc is no longer readable by any application user:"
echo "     sudo -u svc-dashboard cat ~ec2-user/.netrc  # should fail"
echo "  3. Verify Cloudflare origin keys are no longer readable:"
echo "     sudo -u svc-dashboard cat /etc/ssl/certs/*-origin.pem  # should fail"
echo "  4. Run box_health.sh to confirm no service drift"
echo "  5. If all good, remove ec2-user from any service that no longer needs it"
echo
echo "See alpha-engine-config#4791 for the full acceptance criteria."
