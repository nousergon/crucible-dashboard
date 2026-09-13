#!/usr/bin/env bash
# test_box_health_coverage_templates.sh — regression test for
# unmonitored_enabled_services() in box_health.sh, the coverage self-check's
# candidate list (alpha-engine-config-I10629).
#
# Root cause under test: a systemd TEMPLATE unit (`name@.service`) was named as
# an unmonitored enabled service on every tick from 2026-09-12 because
# `systemctl is-enabled` answers `indirect` (exit 0) for a template and
# `systemctl show <template> -p Type` answers empty, not `oneshot`. A template
# cannot run; its instances can, and those must still be caught.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/box_health.sh"
[ -r "$TARGET_SCRIPT" ] || { echo "FAIL - cannot read $TARGET_SCRIPT"; exit 1; }
eval "$(awk '/^unmonitored_enabled_services\(\) \{/,/^\}/' "$TARGET_SCRIPT")"
declare -F unmonitored_enabled_services >/dev/null || { echo "FAIL - unmonitored_enabled_services() not found (extraction failed)"; exit 1; }

FIX=$(mktemp -d); trap 'rm -rf "$FIX"' EXIT
mkdir -p "$FIX/units" "$FIX/bin"
: > "$FIX/units/ops-config-pull@.service"     # template: cannot run
: > "$FIX/units/plain-app.service"            # enabled, Type=simple, unmonitored
: > "$FIX/units/nightly-job.service"          # enabled, Type=oneshot — never named
: > "$FIX/units/covered.service"              # enabled, simple, but monitored

# Fake systemctl: answers exactly what the real one does for these shapes.
# INSTANCES=<names> lists loaded instances of the template.
cat > "$FIX/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
case "$1" in
    is-enabled) case "$3" in *@.service) exit 0 ;; *) exit 0 ;; esac ;;
    show)
        u="$2"
        case "$u" in
            *@.service) echo "" ;;                       # template: empty Type
            nightly-job.service) echo oneshot ;;
            ops-config-pull@alpha-engine-config.service) echo oneshot ;;
            *) echo simple ;;
        esac ;;
    list-units)
        for i in ${INSTANCES:-}; do echo "$i loaded active running"; done ;;
esac
FAKE
chmod +x "$FIX/bin/systemctl"
export PATH="$FIX/bin:$PATH"

FAILURES=0
check() {  # desc expected actual
    if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected '$2', got '$3')"; FAILURES=$((FAILURES+1)); fi
}

out=$(INSTANCES="" unmonitored_enabled_services "$FIX/units" "covered.service")
check "template unit itself is never named; the simple unmonitored one is" "plain-app.service " "$out"

out=$(INSTANCES="ops-config-pull@alpha-engine-config.service" unmonitored_enabled_services "$FIX/units" "covered.service")
check "a oneshot instance of the template is not named" "plain-app.service " "$out"

out=$(INSTANCES="ops-config-pull@rogue.service" unmonitored_enabled_services "$FIX/units" "covered.service plain-app.service")
check "a long-running instance of a template IS named" "ops-config-pull@rogue.service " "$out"

out=$(INSTANCES="ops-config-pull@rogue.service" unmonitored_enabled_services "$FIX/units" "covered.service plain-app.service ops-config-pull@rogue.service")
check "a covered instance is not named" "" "$out"

[ "$FAILURES" -eq 0 ] && { echo "PASS - unmonitored_enabled_services"; exit 0; }
echo "FAIL - $FAILURES case(s)"; exit 1
