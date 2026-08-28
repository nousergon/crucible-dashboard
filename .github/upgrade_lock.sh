#!/usr/bin/env bash
# Move requirements.txt to the newest versions requirements.in still permits.
#
# THE PRODUCER for this repo's compiled lockfile, and the reason the pip
# ecosystem is absent from .github/dependabot.yml (alpha-engine-config-I9060).
# Dependabot edits requirements.txt directly and has no knowledge of
# requirements.in, so every pip PR it opens fails `lockfile-reproducible`
# ("requirements.txt is not reproducible from requirements.in") by
# construction, before a single dependency is graded. Measured on the sibling
# repo the same day (crucible-executor#511, 2026-08-28); the guard here is the
# same guard, landed by crucible-dashboard#774.
#
# A bot that cannot open a green PR is not dependency maintenance; it is a
# queue of red PRs that trains the reader to ignore the ones that matter. And
# the human workaround has already cost deploys here: crucible-dashboard#778
# had to move requirements.in by hand because "every deploy has been failing
# since #775".
#
# Run by nousergon/alpha-engine-config's `lockfile-upgrade-sweep` on a weekly
# schedule, which discovers this file by its CANONICAL PATH — `.github/
# upgrade_lock.sh` — clones the repo, runs it, and opens the PR under the
# ne-groomer App token (a PR opened with the default GITHUB_TOKEN does not
# trigger workflow runs, so it would arrive with no CI at all — the same
# ungradeable-PR failure in a new costume). Also runnable by hand from a
# clean checkout.
#
# Usage:
#   bash .github/upgrade_lock.sh            write requirements.txt in place
#   bash .github/upgrade_lock.sh --check    compile to a temp file, report
#                                           whether an upgrade is available,
#                                           write nothing, exit 0
#
# --check is what CI runs on every PR touching this script, its flags file,
# or requirements.in. A producer nobody ever executes rots into a script that
# fails on its first real run months later (alpha-engine-config-I8729); this
# way the produce path is exercised on the very PR that changes it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=.github/lockfile_compile.sh
source "$ROOT/.github/lockfile_compile.sh"

CHECK=0
if [[ "${1:-}" == "--check" ]]; then
    CHECK=1
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--check]" >&2
    exit 2
fi

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

echo "Recompiling requirements.in under Python ${LOCK_PYVER} with --upgrade..."
lockfile_compile "$OUT" --upgrade --quiet

if diff -q "$ROOT/requirements.txt" "$OUT" >/dev/null; then
    echo "UPGRADE_LOCK_RESULT changed=false"
    exit 0
fi

echo "UPGRADE_LOCK_RESULT changed=true"
diff "$ROOT/requirements.txt" "$OUT" || true

if [[ "$CHECK" -eq 1 ]]; then
    echo "(--check: requirements.txt left untouched)"
    exit 0
fi

cp "$OUT" "$ROOT/requirements.txt"
echo "Wrote requirements.txt."
