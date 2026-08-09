#!/bin/bash
# test_installer_runtime_override_guard.sh — regression test for the
# system.control preflight guard in install-resource-limits.sh
# (alpha-engine-config-I6277).
#
# THE BUG THIS GUARDS
# --------------------
# `systemctl set-property --runtime <unit> MemoryMax=...` writes
# /run/systemd/system.control/<unit>.d/50-Memory{High,Max}.conf, which
# OUTRANKS the /etc drop-in this script generates. Before this guard existed,
# re-running install-resource-limits.sh against a unit with a live --runtime
# override wrote a syntactically fine /etc drop-in that had NO effect on the
# running unit -- `daemon-reload` does not touch /run. Measured live on
# metron-api.service, 2026-08-03 17:11-17:40 UTC: the override was still live
# and paging 30 minutes after the installer had "fixed" it.
#
# WHAT IS ASSERTED
# ----------------
# Per the issue's own gotcha: the guard must be shown to FAIL against an
# unfixed fixture (an override present), not merely pass against a clean one
# -- a check exercised only on the happy path is not evidence it catches
# anything.
#
# BUDGET and RUNTIME_DROPIN_ROOT are both overridable via env var
# specifically so this can run off-box, with no real systemd and no root,
# against a minimal fixture budget.yaml instead of the real 14-service one.
#
# Run directly, or via tests/test_dash_deploy_infra.py under pytest:
#   bash infrastructure/test_installer_runtime_override_guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/install-resource-limits.sh"

FAILURES=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

FIXTURE_BUDGET="$TMP/budget.yaml"
cat > "$FIXTURE_BUDGET" <<'YAML'
ram_mb: 2000
reserve_fraction: 0.15
max_overcommit_ratio: 2.0
max_steady_state_fraction: 0.60
services:
  - unit: fixture-svc.service
    port: 9000
    memory_high: 100M
    memory_max: 150M
YAML

pass_or_fail() {
    local desc="$1" ok="$2"
    if [ "$ok" = "1" ]; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc"
        FAILURES=$((FAILURES + 1))
    fi
}

echo "== clean box: no live override present =="
CLEAN_RUNTIME_ROOT="$TMP/clean-runtime"
mkdir -p "$CLEAN_RUNTIME_ROOT"
out=$(BUDGET="$FIXTURE_BUDGET" RUNTIME_DROPIN_ROOT="$CLEAN_RUNTIME_ROOT" \
      bash "$INSTALLER" --dry-run 2>&1)
rc=$?
[ "$rc" -eq 0 ] && ok=1 || ok=0
pass_or_fail "installer exits 0 with no live override" "$ok"
case "$out" in
    *"REFUSING to write a drop-in"*) ok=0 ;;
    *) ok=1 ;;
esac
pass_or_fail "clean run never mentions the refusal" "$ok"
case "$out" in
    *"would write"*fixture-svc.service*) ok=1 ;;
    *) ok=0 ;;
esac
pass_or_fail "clean run reaches the render step (would write the drop-in)" "$ok"

echo
echo "== a LIVE 'systemctl set-property --runtime' override is present =="
DIRTY_RUNTIME_ROOT="$TMP/dirty-runtime"
mkdir -p "$DIRTY_RUNTIME_ROOT/fixture-svc.service.d"
cat > "$DIRTY_RUNTIME_ROOT/fixture-svc.service.d/50-MemoryMax.conf" <<'CONF'
[Service]
MemoryMax=900M
CONF
out=$(BUDGET="$FIXTURE_BUDGET" RUNTIME_DROPIN_ROOT="$DIRTY_RUNTIME_ROOT" \
      bash "$INSTALLER" --dry-run 2>&1)
rc=$?
[ "$rc" -ne 0 ] && ok=1 || ok=0
pass_or_fail "installer exits non-zero against an unfixed override" "$ok"
case "$out" in
    *"REFUSING to write a drop-in for fixture-svc.service"*) ok=1 ;;
    *) ok=0 ;;
esac
pass_or_fail "refusal names the unit" "$ok"
case "$out" in
    *"$DIRTY_RUNTIME_ROOT/fixture-svc.service.d/50-MemoryMax.conf"*) ok=1 ;;
    *) ok=0 ;;
esac
pass_or_fail "refusal names the exact override path" "$ok"
case "$out" in
    *"systemctl revert fixture-svc.service"*) ok=1 ;;
    *) ok=0 ;;
esac
pass_or_fail "refusal names the exact revert command" "$ok"
case "$out" in
    *"would write"*fixture-svc.service*) ok=0 ;;
    *) ok=1 ;;
esac
pass_or_fail "refusal is a PREFLIGHT -- it never reaches the render step" "$ok"
# Must never auto-revert -- only NAME the command, never execute it. There is
# no `systemctl` on PATH in this sandboxed run at all, so any attempt to
# actually invoke `systemctl revert` would itself fail loudly; absence of
# that failure is not proof, so this also greps the script source directly.
if grep -qE '^\s*systemctl\s+revert\b' "$INSTALLER"; then
    ok=0
else
    ok=1
fi
pass_or_fail "the installer source never itself calls 'systemctl revert'" "$ok"

echo
echo "== a MemoryHigh-only override is also caught =="
HIGH_ONLY_ROOT="$TMP/high-only-runtime"
mkdir -p "$HIGH_ONLY_ROOT/fixture-svc.service.d"
cat > "$HIGH_ONLY_ROOT/fixture-svc.service.d/50-MemoryHigh.conf" <<'CONF'
[Service]
MemoryHigh=700M
CONF
out=$(BUDGET="$FIXTURE_BUDGET" RUNTIME_DROPIN_ROOT="$HIGH_ONLY_ROOT" \
      bash "$INSTALLER" --dry-run 2>&1)
rc=$?
[ "$rc" -ne 0 ] && ok=1 || ok=0
pass_or_fail "a MemoryHigh-only runtime drop-in also refuses" "$ok"

echo
echo "== a drop-in with no memory settings is NOT a false positive =="
IRRELEVANT_ROOT="$TMP/irrelevant-runtime"
mkdir -p "$IRRELEVANT_ROOT/fixture-svc.service.d"
cat > "$IRRELEVANT_ROOT/fixture-svc.service.d/50-Other.conf" <<'CONF'
[Service]
Environment=FOO=bar
CONF
out=$(BUDGET="$FIXTURE_BUDGET" RUNTIME_DROPIN_ROOT="$IRRELEVANT_ROOT" \
      bash "$INSTALLER" --dry-run 2>&1)
rc=$?
[ "$rc" -eq 0 ] && ok=1 || ok=0
pass_or_fail "a non-memory drop-in under system.control does not false-positive" "$ok"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "PASS - all runtime-override guard assertions"
    exit 0
fi
echo "FAILED - $FAILURES assertion(s)"
exit 1
