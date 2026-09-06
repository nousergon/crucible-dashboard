#!/usr/bin/env bash
# Fail when the nousergon-lib VCS ref in requirements.in and requirements.txt
# disagree (alpha-engine-config-I<TBD>).
#
# WHY REF EQUALITY IS REQUIRED, not merely presence+extras. Two files declare
# the same git pin and two different consumers read two different ones:
#
#   requirements.txt  is what `pip install -r` puts in the venv, so it decides
#                     the INSTALLED version.
#   requirements.in   is what `infrastructure/check_package_drift.py` reads —
#                     deliberately, per its own docstring: the .in file is the
#                     hand-authored source of truth and is immune to unrelated
#                     transitive-pin churn. `LibPinDriftCheck` in the weekly SF
#                     resolves the dashboard pin from the .in file too.
#
# check_package_drift then compares the .in tag against the installed version.
# So the moment the lock moves ahead of the .in — a hand-edited lock, a
# Dependabot-shaped producer that never reads the .in, a bump applied to one
# half — the box's own preflight refuses EVERY deploy, with CI fully green,
# because CI never installs and never compares the two refs.
#
# That is not hypothetical; it is the third recurrence in this repo:
#
#   #739   2026-08-20  .txt bumped, .in left behind. Three merges landed on
#                      main and none reached the box.
#   #775   2026-08-22  .txt moved to the v0.124.88 SHA, .in left at v0.124.86.
#                      Every deploy failed until 2026-08-25.
#   #813   2026-08-31  .txt moved to v0.124.104, .in left at v0.124.102.
#                      deploy.yml FAILED on 8 of 8 runs, 2026-09-02..09-06;
#                      box_health.sh's identity fix (#827) merged and never
#                      became live.
#
# `check_lock_reproducible.sh` could not catch any of them by construction: it
# compares the `nousergon-lib @ git+...` line by PRESENCE and EXTRAS only,
# never by ref, because a compile is free to resolve a tag to the commit SHA it
# points at. That freedom is preserved here — REF EQUALITY, not tag-shaped-ness:
# a raw SHA on both sides passes, a tag on both sides passes, and only a
# genuine disagreement fails. What it forbids is the two halves of one pin
# naming different code.
#
# Usage:
#   bash infrastructure/check_lib_ref_parity.sh [requirements.in] [requirements.txt]
# Exit 0 = refs identical; 1 = mismatch, missing pin, or unreadable file.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REQ_IN="${1:-$ROOT/requirements.in}"
REQ_TXT="${2:-$ROOT/requirements.txt}"

# The ref is everything after the LAST `@` of the git URL: `@v0.124.104`,
# `@68d643424ddef7f38648d3919842b1bbe7aa731a`, `@main`. Trailing whitespace and
# an inline `# comment` are stripped so a commented lock line compares equal to
# a bare one. A line with no `@ref` at all yields the empty string, which is
# reported as a missing ref rather than silently matching another empty one.
extract_lib_ref() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        echo "MISSING_FILE"
        return
    fi
    local line
    line="$(grep -E '^nousergon-lib(\[[^]]*\])? @ git\+' "$file" | head -n1 || true)"
    if [[ -z "$line" ]]; then
        echo "NO_PIN"
        return
    fi
    line="${line%%#*}"
    line="${line%"${line##*[![:space:]]}"}"
    local url="${line#*@ }"
    if [[ "$url" != *"@"* ]]; then
        echo "NO_REF"
        return
    fi
    echo "${url##*@}"
}

in_ref="$(extract_lib_ref "$REQ_IN")"
txt_ref="$(extract_lib_ref "$REQ_TXT")"

for pair in "requirements.in:$in_ref:$REQ_IN" "requirements.txt:$txt_ref:$REQ_TXT"; do
    label="${pair%%:*}"
    rest="${pair#*:}"
    ref="${rest%%:*}"
    path="${rest#*:}"
    case "$ref" in
        MISSING_FILE)
            echo "FAIL: $label not found at $path — cannot compare the"
            echo "      nousergon-lib pin. Refusing to silently pass."
            exit 1
            ;;
        NO_PIN)
            echo "FAIL: $label ($path) carries no nousergon-lib git pin."
            exit 1
            ;;
        NO_REF)
            echo "FAIL: $label ($path) pins nousergon-lib with no @ref — an"
            echo "      unpinned git URL resolves to the default branch, which"
            echo "      is a different package on every install."
            exit 1
            ;;
    esac
done

if [[ "$in_ref" != "$txt_ref" ]]; then
    echo "FAIL: the two halves of the nousergon-lib pin name different code."
    echo "      requirements.in  ref: $in_ref"
    echo "      requirements.txt ref: $txt_ref"
    echo
    echo "      requirements.txt decides what is INSTALLED; requirements.in is"
    echo "      what infrastructure/check_package_drift.py reads on the box."
    echo "      While they disagree, deploy-on-merge.sh's drift gate refuses"
    echo "      EVERY deploy — merges land on main and never reach the box —"
    echo "      and CI stays green throughout, because CI never installs."
    echo
    echo "Fix, whichever is the stale half:"
    echo "  - repoint requirements.in's nousergon-lib line to $txt_ref, or"
    echo "  - recompile the lock: $ROOT/.github/upgrade_lock.sh"
    echo "    (or the exact command in infrastructure/check_lock_reproducible.sh)"
    echo "  and update tests/test_flow_doctor_wiring.py's asserted tag to match."
    exit 1
fi

echo "OK: nousergon-lib ref matches between requirements.in and requirements.txt ($in_ref)."
