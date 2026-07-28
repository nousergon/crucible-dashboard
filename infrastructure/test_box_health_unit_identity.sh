#!/bin/bash
# test_box_health_unit_identity.sh — regression test for classify_identity() in
# box_health.sh, the check that a unit's User=/Group= can actually be resolved.
#
# Root cause under test: a unit whose User= does not resolve cannot start.
# systemd fails it at step USER with 217/USER, before ExecStart runs. Critically
# this is invisible to `systemctl is-active` — a service already running when
# its User= became unresolvable keeps reporting active until something restarts
# it.
#
# On 2026-07-28 thirteen dashboard-box units were given User=svc-<name> for
# accounts nothing had created (nous-ergon-ops-I155). The five the deploy
# restarted died at once; the other eight reported healthy and were one restart
# — reboot-if-needed.timer makes that unattended — from taking the whole box
# down. box_health.sh could not tell those eight from genuinely healthy
# services, because "is it running" and "could it start again" are different
# questions and only the first was being asked.
#
# WHY THE EMPTY CASE IS THE ONE THAT MATTERS HERE
# The incident this check comes from is the 15th instance of
# guard-filter-excludes-the-class-it-protects: a guard whose filter excludes
# exactly what it exists to catch. The obvious way to write this check wrong is
# to treat an unset User= as unresolvable. Most units on the box have no User=
# (they inherit root) and are perfectly healthy, so that version would flag ~90%
# of units on its first run, be tuned down within a day, and the tuning would
# very plausibly take the real signal with it. The empty case is therefore
# asserted explicitly, not left to inspection.
#
# classify_identity() is a pure function of its arguments so this can be
# asserted without systemd or a real /etc/passwd. Invoked by
# tests/test_dash_deploy_infra.py so CI actually runs it; also runnable directly:
#   bash infrastructure/test_box_health_unit_identity.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_health.sh"

if [ ! -r "$TARGET_SCRIPT" ]; then
    echo "FAIL - cannot read $TARGET_SCRIPT"
    exit 1
fi

# Source only the function under test. box_health.sh runs checks at load time,
# so extract the definition rather than sourcing the whole script.
fn=$(awk '/^classify_identity\(\) \{/,/^\}/' "$TARGET_SCRIPT")
if [ -z "$fn" ]; then
    echo "FAIL - classify_identity() not found in box_health.sh"
    exit 1
fi
eval "$fn"

PASS=0
FAIL=0

# expect_finding DESC UNIT FIELD VALUE RESOLVES EXPECT_MATCH
#   EXPECT_MATCH empty  => must emit NOTHING
#   EXPECT_MATCH set    => output must contain that substring
expect() {
    local desc="$1" unit="$2" field="$3" value="$4" resolves="$5" want="$6"
    local got
    got=$(classify_identity "$unit" "$field" "$value" "$resolves")
    if [ -z "$want" ]; then
        if [ -z "$got" ]; then
            PASS=$((PASS + 1)); echo "  ok   $desc"
        else
            FAIL=$((FAIL + 1)); echo "  FAIL $desc — expected silence, got: $got"
        fi
    else
        if [[ "$got" == *"$want"* ]]; then
            PASS=$((PASS + 1)); echo "  ok   $desc"
        else
            FAIL=$((FAIL + 1)); echo "  FAIL $desc — expected to contain '$want', got: ${got:-<nothing>}"
        fi
    fi
}

echo "classify_identity() — the check must FAIL on what it exists to catch:"

# THE NEGATIVE FIXTURE. This is the exact shape of the 2026-07-28 outage: a
# unit configured with a per-service account that was never created. If this
# case ever stops producing a finding, the check has been silenced.
expect "unresolvable User= is flagged (the I155 shape)" \
    "dashboard.service" "User" "svc-dashboard" "no" \
    "unit cannot restart: dashboard.service has User=svc-dashboard"
expect "unresolvable Group= is flagged" \
    "metron-api.service" "Group" "svc-metron" "no" \
    "unit cannot restart: metron-api.service has Group=svc-metron"

echo "classify_identity() — and must stay silent on healthy shapes:"

# The case that would make this check unusable if got wrong. Unset User= means
# the unit inherits root; that is the majority of units and is healthy.
expect "unset User= is silent (inherits root — the majority case)" \
    "nginx.service" "User" "" "no" ""
expect "unset Group= is silent" \
    "nginx.service" "Group" "" "no" ""
expect "resolvable User= is silent" \
    "dashboard.service" "User" "ec2-user" "yes" ""
expect "resolvable Group= is silent" \
    "dashboard.service" "Group" "ec2-user" "yes" ""

# An unset value must be silent regardless of what the resolver reported —
# getent on an empty string is not meaningful and must never drive a finding.
expect "unset User= is silent even if resolver says yes" \
    "vires.service" "User" "" "yes" ""

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "PASS - classify_identity() flags unresolvable identities and only those"
