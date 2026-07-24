#!/usr/bin/env python3
"""check_package_drift.py — package-version-drift preflight (config#3157).

``deploy-on-merge.sh``'s requirements-install step (config#2338) is
STATE-COMPARE gated: it only runs ``pip install`` when ``requirements.txt``
differs from a stamp file recording what was last installed. That gate
protects against a *missed* install, but nothing on this box ever asked the
converse question — does the venv's INSTALLED version of a fleet-wide
shared lib actually satisfy what the repo currently declares? A stamp can
match, a pip install can succeed, and the box can still end up on a stale
version if e.g. a manual ``pip install`` happened out of band, a venv was
copied from an older box, or the self-heal venv-rebuild path
(``deploy-on-merge.sh`` §0) restored packages from a requirements.txt that
predates the one now in the repo.

This is exactly the shape of the 2026-07 incident this closes: the box ran
``krepis`` 0.14.0 for days after ``requirements.in``'s source-of-truth floor
said ``krepis[openai]>=0.16.2`` — caught only by a human manually running
``krepis.__version__``, not by any automated check.

Mirrors ``crucible-executor/executor/preflight.py``'s
``check_deploy_drift`` git-checkout override in POSTURE (fail-loud,
descriptive RuntimeError naming exactly what's stale and why) but checks
PACKAGE versions instead of a git SHA: read the declared floor/pin for a
package straight from ``requirements.in`` (the hand-authored source of
truth; ``requirements.txt`` is its compiled lockfile — either works as the
constraint source, but ``.in`` is what a human actually wrote and is
immune to unrelated transitive-pin churn), compare against the INSTALLED
version via ``importlib.metadata.version()``, and hard-fail on a
specifier violation.

Usage (called from ``infrastructure/deploy-on-merge.sh`` right after the
pip-install step, using the box venv's own interpreter so the installed
versions checked are the ones actually live for the running services):

    .venv/bin/python infrastructure/check_package_drift.py

Exit codes: 0 = no drift (or package/requirement not found — see below),
1 = drift detected (installed version violates the declared floor/pin) or
the requirements file itself is unreadable/unparseable, so this fails
loud rather than silently passing on a broken source file.

Scope (config#3157): starts with ``krepis`` and ``nousergon-lib``, the two
fleet-wide shared libs the issue names. Add more entries to
``_CHECKED_PACKAGES`` as other shared libs earn the same guard.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

# The two fleet-wide shared libs config#3157 names. Both are declared in
# requirements.in with a real PEP 508 specifier line — krepis as a floor
# (`krepis[openai]>=0.16.2`), nousergon-lib as an exact VCS tag pin
# (`nousergon-lib[...] @ git+...@vX.Y.Z`), handled separately below since a
# VCS URL isn't a version specifier `packaging.requirements` can compare.
_CHECKED_PACKAGES = ("krepis", "nousergon-lib")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS_IN = _REPO_ROOT / "requirements.in"

# Matches a VCS-pinned line's trailing `@vX.Y.Z` tag, e.g.:
#   nousergon-lib[flow-doctor,github_app] @ git+https://.../nousergon-lib@v0.124.0
_VCS_TAG_RE = re.compile(r"@v(?P<version>[0-9][0-9A-Za-z.\-]*)\s*$")


def _iter_requirement_lines(path: Path) -> list[str]:
    """Return requirements.in's non-comment, non-blank logical lines.

    requirements.in has no line continuations in this repo, so a plain
    per-line strip-and-filter is sufficient (no PEP 508 continuation
    handling needed).
    """
    lines: list[str] = []
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _find_declared_constraint(pkg: str, lines: list[str]) -> str | None:
    """Return the raw requirement line declaring ``pkg``, or None if absent.

    Matches on the distribution name at the start of the line (before any
    ``[extras]``, version specifier, or ``@`` VCS marker), case-insensitively
    and normalizing ``_``/``-`` (PEP 503), so ``nousergon_lib`` and
    ``nousergon-lib`` are treated as the same package.
    """
    normalized_pkg = pkg.replace("_", "-").lower()
    for line in lines:
        name_part = re.split(r"[\[\s@=<>!~;]", line, maxsplit=1)[0]
        if name_part.replace("_", "-").lower() == normalized_pkg:
            return line
    return None


def _installed_version(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def check_package_drift(
    packages: tuple[str, ...] = _CHECKED_PACKAGES,
    requirements_in: Path = _REQUIREMENTS_IN,
) -> list[str]:
    """Return a list of human-readable drift-violation messages (empty = OK).

    Raises RuntimeError if ``requirements_in`` is missing or a declared
    line can't be parsed at all — a broken source-of-truth file should
    fail loud, not be silently skipped.
    """
    if not requirements_in.is_file():
        raise RuntimeError(
            f"check_package_drift: {requirements_in} not found — cannot "
            f"read declared package floors. Refusing to silently pass."
        )
    lines = _iter_requirement_lines(requirements_in)

    violations: list[str] = []
    for pkg in packages:
        declared = _find_declared_constraint(pkg, lines)
        if declared is None:
            # Not declared at all in requirements.in — nothing to check
            # against. Not a drift condition by itself (a package could be
            # a transitive-only dependency); skip rather than fail loud.
            continue

        installed = _installed_version(pkg)
        if installed is None:
            violations.append(
                f"{pkg}: declared in requirements.in ({declared!r}) but NOT "
                f"INSTALLED in this venv at all — pip install did not "
                f"bring it in, or it was removed out of band."
            )
            continue

        vcs_match = _VCS_TAG_RE.search(declared)
        if vcs_match:
            # VCS-pinned exact tag (e.g. nousergon-lib @ git+...@v0.124.0):
            # compare the installed version against the tag's version
            # component directly rather than via packaging.requirements
            # (which doesn't parse specifiers out of a `@ <url>` line).
            pinned = vcs_match.group("version")
            try:
                if Version(installed) != Version(pinned):
                    violations.append(
                        f"{pkg}: requirements.in pins tag v{pinned} but the "
                        f"installed version is {installed} — venv is on the "
                        f"WRONG commit/version of a VCS-pinned fleet-wide "
                        f"lib. pip install requirements.txt did not take, or "
                        f"the install predates the current pin."
                    )
            except InvalidVersion:
                violations.append(
                    f"{pkg}: could not compare installed version {installed!r} "
                    f"against pinned tag v{pinned!r} (unparseable version) — "
                    f"treating as drift out of caution."
                )
            continue

        # Plain PEP 508 specifier (e.g. krepis[openai]>=0.16.2): let
        # packaging do the real specifier-satisfaction check rather than a
        # hand-rolled string/tuple compare, so it's correct for any operator
        # (>=, ==, ~=, etc.), not just floors.
        try:
            req = Requirement(declared)
        except InvalidRequirement as exc:
            raise RuntimeError(
                f"check_package_drift: could not parse requirements.in line "
                f"for {pkg!r}: {declared!r} ({exc})"
            ) from exc

        try:
            installed_v = Version(installed)
        except InvalidVersion:
            violations.append(
                f"{pkg}: installed version {installed!r} is not a parseable "
                f"version — cannot verify against {req.specifier}; treating "
                f"as drift out of caution."
            )
            continue

        if req.specifier and installed_v not in req.specifier:
            violations.append(
                f"{pkg}: requirements.in requires {req.specifier} but the "
                f"INSTALLED version is {installed} — this is the "
                f"config#3157 / krepis-0.14-vs-0.16.2 drift class: the venv "
                f"is running a package version older (or otherwise "
                f"non-conforming) than the repo's declared floor, with no "
                f"pip install having corrected it. Rebuild the venv or run "
                f"`pip install -r requirements.txt` to resync."
            )

    return violations


def main() -> int:
    try:
        violations = check_package_drift()
    except RuntimeError as exc:
        print(f"FAIL check_package_drift: {exc}", file=sys.stderr)
        return 1

    if violations:
        print(
            "FAIL check_package_drift: package-version drift detected "
            f"({len(violations)} violation(s)):",
            file=sys.stderr,
        )
        for msg in violations:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print(f"OK   check_package_drift: {', '.join(_CHECKED_PACKAGES)} match requirements.in ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
