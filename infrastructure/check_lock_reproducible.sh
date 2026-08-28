#!/usr/bin/env bash
# Fail when requirements.txt is not what `uv pip compile requirements.in`
# produces (alpha-engine-config-I8309).
#
# WHY THIS EXISTS. requirements.txt declares in its own header that it is
# compiled from requirements.in. Nothing enforced that. The lock was
# hand-edited forward twice in one day (crucible-dashboard-PR772 flow-doctor
# 0.15.2 -> 0.16.0, PR773 krepis 0.59.31 -> 0.59.33) because a full recompile
# was not trusted, and each hand edit left a comment saying so. Before that,
# crucible-dashboard#739 bumped requirements.txt and left requirements.in
# behind, which failed check_package_drift on the box for every deploy from
# 2026-08-20 22:45 while CI stayed green — three merges landed on main and
# none of them reached the box. The sibling repo's version of this drift ran
# further: crucible-executor's .in floored krepis>=0.59.18 against a lock
# pinning 0.54.0, with the .in outright UNSATISFIABLE, because no check ever
# ran the compile (crucible-executor-PR493).
#
# `lockfile-python-parity` INSTALLS the lock and proves it resolves under the
# SSoT interpreter. That is a strictly weaker claim than this one and it
# cannot see a floor raised in requirements.in at all.
#
# MEASURED HERE, 2026-08-24: `setuptools>=84.0.0` — declared in
# requirements.in solely to satisfy the pip-audit gate for PYSEC-2026-3447 —
# was absent from the committed lock entirely, so the security floor the
# .in declares was never in the file the box installs.
#
# THE COMPILE FLAGS ARE LOAD-BEARING, all three:
#
#   --python-platform x86_64-unknown-linux-gnu
#       streamlit requires `watchdog` only where platform_system != "Darwin".
#       Resolving on a macOS laptop DROPS watchdog; resolving on the ubuntu
#       runner keeps it. Without this flag the check is red or green
#       depending on whose machine last ran it, which is not a check. Linux
#       is also the only platform this repo deploys to (the EC2 box) and the
#       one nousergon-data's CI installs this lock on.
#
#   --no-strip-extras
#       uv strips extras from output lines by default. That would rewrite
#       `krepis[flow-doctor,openai]==` to `krepis==` and
#       `nousergon-lib[flow-doctor,github-app] @ ...` to `nousergon-lib @ ...`.
#       The resolved package SET is identical either way (each extra's deps
#       are pinned on their own lines), but a `pip install -r` of a stripped
#       line installs the package WITHOUT its extras, and the extras here are
#       load-bearing: [openai] backs live/morning_brief.py's OpenRouter
#       transport, [flow-doctor] backs the FlowDoctorHandler, [github_app]
#       backs the Decision Queue loader's App auth. Keeping them also means
#       this check FAILS if a future recompile silently strips them.
#
#   --python "$(cat .python-version)"
#       the SSoT interpreter, same source `lockfile-python-parity` uses.
#
# THE RECOMPILE IS SEEDED WITH THE CURRENT LOCK, deliberately. `uv pip
# compile` treats an existing output file as preferred versions and holds
# them unless a constraint forces a move. Compiling into an EMPTY temp file
# instead resolves everything to newest-on-PyPI, so the check would go red
# every time any transitive dependency cut a release — a detector that fails
# for a reason nobody in this repo caused is a detector that gets ignored.
# Seeded, it answers the question that matters: do the pins we ship still
# satisfy the constraints we declare?
#
# WHAT IS COMPARED. Every pinned line, INCLUDING its extras
# (`name[extra,...]==version`), as a set. The `nousergon-lib @ git+...` line
# is compared by PRESENCE and EXTRAS only, never by ref: a compile always
# resolves the vX.Y.Z tag in requirements.in to the commit SHA it points at,
# by design (a tag can be force-moved, a commit cannot). requirements.in is
# where the human-authored tag lives, and
# tests/test_flow_doctor_wiring.py::test_requirements_in_pins_lib_to_stable_tag
# owns that boundary — it asserts the exact tag and forbids `@main`.
# `crucible-predictor/inference/lib_pin_drift.py` reads NOTHING for this
# repo (it is in neither `_CO_INSTALL_PAIR` nor `_FLOOR_REPOS`), so the SHA
# in this lock puts nothing outside a cross-repo check.
#
# THE FLAGS ABOVE LIVE IN ONE FILE, `.github/lockfile_compile.sh`, shared with
# `.github/upgrade_lock.sh` — the producer that replaced Dependabot's pip
# ecosystem here (alpha-engine-config-I9060). A verifier and a producer that
# each carry their own copy of the flags drift into a lock this check can
# never pass, which is exactly the failure being closed; they cannot drift
# when there is one copy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=.github/lockfile_compile.sh
source "$ROOT/.github/lockfile_compile.sh"
PYVER="$LOCK_PYVER"
PLATFORM="$LOCK_PLATFORM"
FRESH="$(mktemp)"
trap 'rm -f "$FRESH" "$FRESH.err"' EXIT

echo "Recompiling requirements.in under Python ${PYVER} for ${PLATFORM}..."
cp requirements.txt "$FRESH"     # seed: hold current pins unless a constraint moves them
if ! lockfile_compile "$FRESH" --quiet 2>"$FRESH.err"; then
    echo "FAIL: requirements.in does not resolve at all under Python ${PYVER}."
    echo "      The lockfile cannot be regenerated, so every floor raised in"
    echo "      requirements.in is unreachable by the deployed environment."
    echo
    cat "$FRESH.err"
    exit 1
fi

# `name[extras]==version`, extras included so a stripped recompile is caught.
pins() { grep -E '^[A-Za-z0-9_.-]+(\[[^]]*\])?==' "$1" | sort; }

if diff_out="$(diff <(pins requirements.txt) <(pins "$FRESH"))"; then
    echo "OK: requirements.txt matches a fresh compile of requirements.in."
else
    echo "FAIL: requirements.txt is not reproducible from requirements.in."
    echo "      '<' is what is COMMITTED (and deployed); '>' is what"
    echo "      requirements.in actually resolves to today."
    echo
    echo "$diff_out"
    echo
    echo "Fix: $(lockfile_compile_hint)"
    echo "     then run the suite against the resolved environment before pushing."
    exit 1
fi

lib_line="$(grep -E '^nousergon-lib(\[[^]]*\])? @ git\+' requirements.txt || true)"
if [[ -z "$lib_line" ]]; then
    echo "FAIL: requirements.txt carries no nousergon-lib git pin."
    exit 1
fi
for extra in flow-doctor github-app; do
    if [[ "$lib_line" != *"$extra"* ]]; then
        echo "FAIL: the nousergon-lib lock line dropped the [${extra}] extra:"
        echo "      $lib_line"
        echo "      Recompile with --no-strip-extras."
        exit 1
    fi
done
echo "OK: the nousergon-lib git pin is present with both extras (its ref form"
echo "    is owned by tests/test_flow_doctor_wiring.py, which asserts the tag"
echo "    in requirements.in)."
