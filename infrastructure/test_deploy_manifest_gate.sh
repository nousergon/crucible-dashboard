#!/bin/bash
# test_deploy_manifest_gate.sh — regression test for manifest_stale() in
# deploy-on-merge.sh.
#
# The bug it guards, found live 2026-07-28:
#
# /etc/alpha-engine/box-services.conf is DERIVED from budget.yaml. The §2e
# deploy gate compared src:dst FILE PAIRS, so a change that only edits
# budget.yaml — a new service, a port, a timer threshold — left every compared
# file byte-identical. The installer never ran, and the watchdog kept its
# previous registry indefinitely.
#
# That is not theoretical. config-I5209 merged, deployed green, and installed
# an updated box_health.sh; the manifest on the box still contained zero of the
# timer thresholds that PR added. The whole premise of I4492 — "one list:
# adding a service to the budget adds it to the watchdog" — was defeated by a
# propagation step nothing automated ever ran (install-resource-limits.sh was
# the sole generator and is invoked by no workflow, no deploy path, no CI).
#
# The fix compares DESIRED (rendered) against ACTUAL (installed) rather than
# comparing inputs, which is what makes it immune to this whole class.
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest:
#   bash infrastructure/test_deploy_manifest_gate.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy-on-merge.sh"

FAILURES=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Extract manifest_stale() and its MANIFEST_DST default, without executing the
# rest of deploy-on-merge.sh (which mutates the live box).
BOX_HEALTH_INFRA="$SCRIPT_DIR"
eval "$(awk '/^MANIFEST_DST=/,/^\}/' "$DEPLOY_SCRIPT")"

if ! declare -F manifest_stale >/dev/null; then
    echo "FAIL - manifest_stale() not found in deploy-on-merge.sh (extraction failed)"
    exit 1
fi

check() {
    local desc="$1" want="$2"   # want: stale | fresh
    local got
    if manifest_stale; then got=stale; else got=fresh; fi
    if [ "$got" = "$want" ]; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc (expected $want, got $got)"
        FAILURES=$((FAILURES + 1))
    fi
}

python3 "$SCRIPT_DIR/generate-box-manifest.py" --stdout > "$TMP/current" 2>/dev/null

echo "== the installed copy matching the rendered one is FRESH =="
MANIFEST_DST="$TMP/current"
check "installed manifest byte-identical to rendered" fresh

echo "== every way it can drift must read STALE =="
MANIFEST_DST="$TMP/missing"
check "installed manifest absent (first deploy)" stale

# THE REGRESSION: a budget.yaml-only change. Simulated by an installed copy
# that is a previous render — exactly what was on the box after PR569.
grep -v 'TIMER_MAX_STALENESS\|^    \["' "$TMP/current" > "$TMP/old"
MANIFEST_DST="$TMP/old"
check "installed manifest predates a budget.yaml-only change" stale

printf '' > "$TMP/empty"
MANIFEST_DST="$TMP/empty"
check "installed manifest truncated to empty" stale

cp "$TMP/current" "$TMP/extra"; echo "SERVICES=(hand-edited)" >> "$TMP/extra"
MANIFEST_DST="$TMP/extra"
check "installed manifest hand-edited (drift)" stale

echo "== a broken generator must fail SAFE (stale), never silently fresh =="
# A render failure reading as "up to date" would skip the installer forever.
BOX_HEALTH_INFRA="$TMP/nonexistent"
MANIFEST_DST="$TMP/current"
check "generator missing -> stale, so the installer runs and fails loudly" stale
BOX_HEALTH_INFRA="$SCRIPT_DIR"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "PASS - all manifest_stale assertions"
    exit 0
fi
echo "FAILED - $FAILURES assertion(s)"
exit 1
