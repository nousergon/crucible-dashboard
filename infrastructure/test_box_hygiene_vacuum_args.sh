#!/bin/bash
# test_box_hygiene_vacuum_args.sh — regression test for
# journald_effective_value() in box_hygiene.sh (alpha-engine-config-I10612).
#
# The bug it guards: box_hygiene.sh ran `journalctl --vacuum-size=100M` on a
# fixed weekly timer regardless of the box's actual journald configuration.
# That was written for an UNCAPPED journald (config#2227, 2026-07-11 outage);
# once nous-ergon-ops installed a 7-day/1500M retention floor
# (/etc/systemd/journald.conf.d/zz-retention.conf, alpha-engine-config-I10253),
# journald enforced that floor itself and the hardcoded 100M vacuum became the
# ONLY thing still cutting the journal down — it threw away 626MB of real
# history on 2026-09-13 that the retention floor was supposed to guarantee.
#
# journald_effective_value() reads `systemd-analyze cat-config`-shaped text
# from stdin and returns the LAST declared value of the given key — the same
# "last file wins" rule systemd itself applies across journald.conf.d/*.conf,
# so a box's own effective config (not a number in this repo) drives the
# vacuum. It is a pure function of stdin, tested here without systemd or a
# real box.
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest:
#   bash infrastructure/test_box_hygiene_vacuum_args.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_hygiene.sh"

if [ ! -r "$TARGET_SCRIPT" ]; then
    echo "FAIL - cannot read $TARGET_SCRIPT"
    exit 1
fi

# Source only the function under test. box_hygiene.sh runs its hygiene pass
# at load time, so extract the definition rather than sourcing the whole
# script (same pattern as test_box_health_unit_identity.sh's
# classify_identity()).
fn=$(awk '/^journald_effective_value\(\) \{/,/^\}/' "$TARGET_SCRIPT")
if [ -z "$fn" ]; then
    echo "FAIL - journald_effective_value() not found in box_hygiene.sh"
    exit 1
fi
eval "$fn"

PASS=0
FAIL=0

# expect DESC KEY WANT <<< CAT_CONFIG_TEXT
expect() {
    local desc="$1" key="$2" want="$3"
    local got
    got=$(journald_effective_value "$key")
    if [ "$got" = "$want" ]; then
        PASS=$((PASS + 1)); echo "  ok   $desc"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL $desc — expected '$want', got '${got}'"
    fi
}

echo "journald_effective_value() — last declared value wins, same as journald:"

# Single file, no drop-ins: the measured 2026-07-11 shape (before I10253).
expect "single [Journal] block, no drop-ins" SystemMaxUse "" <<'EOF'
# /etc/systemd/journald.conf
[Journal]
#SystemMaxUse=
EOF

# The exact measured shape on 2026-09-13: size-cap.conf (100M) loaded before
# zz-retention.conf (1500M) — zz- sorts last, so its value must win.
expect "size-cap.conf then zz-retention.conf: SystemMaxUse" SystemMaxUse "1500M" <<'EOF'
# /etc/systemd/journald.conf
[Journal]
#SystemMaxUse=

# /etc/systemd/journald.conf.d/size-cap.conf
[Journal]
SystemMaxUse=100M

# /etc/systemd/journald.conf.d/zz-retention.conf
[Journal]
SystemMaxUse=1500M
MaxRetentionSec=7day
EOF

expect "size-cap.conf then zz-retention.conf: MaxRetentionSec" MaxRetentionSec "7day" <<'EOF'
# /etc/systemd/journald.conf
[Journal]
#SystemMaxUse=

# /etc/systemd/journald.conf.d/size-cap.conf
[Journal]
SystemMaxUse=100M

# /etc/systemd/journald.conf.d/zz-retention.conf
[Journal]
SystemMaxUse=1500M
MaxRetentionSec=7day
EOF

# Neither key declared anywhere: must come back empty so the caller vacuums
# nothing rather than falling back to a hardcoded number.
expect "neither key declared -> empty" SystemMaxUse "" <<'EOF'
# /etc/systemd/journald.conf
[Journal]
#Storage=persistent
EOF

expect "neither key declared -> empty (MaxRetentionSec)" MaxRetentionSec "" <<'EOF'
# /etc/systemd/journald.conf
[Journal]
#Storage=persistent
EOF

# A later file explicitly resetting to default (empty RHS) must win over an
# earlier real value — that IS systemd's own "reset to default" grammar, and
# treating it as "ignore, keep the old value" would silently un-reset it.
expect "later empty assignment resets to unset" SystemMaxUse "" <<'EOF'
# /etc/systemd/journald.conf.d/size-cap.conf
[Journal]
SystemMaxUse=100M

# /etc/systemd/journald.conf.d/zz-override.conf
[Journal]
SystemMaxUse=
EOF

# Inline comments and surrounding whitespace must not leak into the value.
expect "inline comment and whitespace stripped" SystemMaxUse "250M" <<'EOF'
[Journal]
  SystemMaxUse = 250M   # trailing note
EOF

echo ""
echo "journald_effective_value: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
