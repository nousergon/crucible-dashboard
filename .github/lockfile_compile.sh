#!/usr/bin/env bash
# SINGLE SOURCE OF TRUTH for the requirements.in -> requirements.txt compile.
#
# Sourced by exactly two call sites, which must never disagree:
#
#   infrastructure/check_lock_reproducible.sh   VERIFIES the committed lock is
#                                               what requirements.in compiles to.
#   .github/upgrade_lock.sh                     PRODUCES that lock.
#
# A flag present in the verifier and absent in the producer makes every
# produced lockfile unverifiable by the guard — which is the shape of
# alpha-engine-config-I9060: Dependabot produced requirements.txt with no
# knowledge of requirements.in at all, so `lockfile-reproducible` fails every
# pip PR it opens by construction, and the standing auto-merge exception for
# Dependabot PRs can never fire in this repo. Keeping the flags in one file is
# what stops the replacement producer inheriting the same defect quietly.
#
# Defines: LOCK_REPO_ROOT, LOCK_PYVER, LOCK_PLATFORM, LOCK_COMPILE_FLAGS[],
# lockfile_compile(), lockfile_compile_hint(). Sets no shell options — both
# callers set `-euo pipefail` themselves.

LOCK_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_PYVER="$(cat "$LOCK_REPO_ROOT/.python-version")"
LOCK_PLATFORM="x86_64-unknown-linux-gnu"

# ALL THREE FLAGS ARE LOAD-BEARING. The reasoning lives in
# infrastructure/check_lock_reproducible.sh's header, which is where a reader
# arrives from a failing check; in summary:
#
#   --python-platform  streamlit requires `watchdog` only where
#                      platform_system != "Darwin", so a resolution on a
#                      laptop DROPS it. Linux is also the only platform this
#                      repo deploys to.
#   --no-strip-extras  uv strips extras by default, which would rewrite
#                      `krepis[flow-doctor,openai]==` to `krepis==`; a
#                      `pip install -r` of the stripped line installs the
#                      package WITHOUT its extras, and every extra here is
#                      load-bearing.
#   --python           the SSoT interpreter, same source
#                      `lockfile-python-parity` uses.
LOCK_COMPILE_FLAGS=(
    --python "$LOCK_PYVER"
    --python-platform "$LOCK_PLATFORM"
    --no-strip-extras
)

# The exact command a human should run to regenerate the lock by hand, and the
# provenance header uv stamps into the file. Pure.
lockfile_compile_hint() {
    echo "uv pip compile requirements.in --output-file requirements.txt ${LOCK_COMPILE_FLAGS[*]}"
}

# lockfile_compile <output-file> [extra uv flags...]
#
# The caller owns the seeding decision: the verifier copies the committed lock
# into <output-file> first (hold current pins unless a constraint moves them);
# the producer passes --upgrade (move every pin to the newest release
# requirements.in still permits).
#
# UV_CUSTOM_COMPILE_COMMAND pins the provenance header uv writes into the
# lockfile. Without it the header records the ABSOLUTE paths of whatever
# checkout and temp file produced it, so requirements.txt would churn on every
# run and differ between a laptop and a runner — a diff that says nothing
# about dependencies, on the one file a reviewer is supposed to read.
lockfile_compile() {
    local out="$1"
    shift
    UV_CUSTOM_COMPILE_COMMAND="$(lockfile_compile_hint)" \
    uv pip compile "$LOCK_REPO_ROOT/requirements.in" \
        --output-file "$out" \
        "${LOCK_COMPILE_FLAGS[@]}" \
        "$@"
}
