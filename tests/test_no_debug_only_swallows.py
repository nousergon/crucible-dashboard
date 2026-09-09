"""Class guard: no `except Exception` handler in this repo's core source
directories may swallow the failure into `logger.debug(...)` or a bare
`pass`, with nothing else recording it (alpha-engine-config-I10226,
following `crucible-executor`'s original `alpha-engine-config-I10031`,
`crucible-executor-PR547`).

The detector itself lives in `nousergon_lib.testing.debug_swallow_guard`
(lifted on second adoption per `policy-shared-code`, `nousergon-lib-PR398`)
-- this file is a thin per-repo call-site: it names the directories that
hold this repo's core source, diffs the live scan against
`.debug-swallow-allowlist.yaml`, and fails the build on drift. See that
module's docstring for the exact AST shape matched.

This repo is a **read-only Streamlit dashboard** (`AGENTS.md`): "every
chart sources from an existing module's output; no metric is computed
ad-hoc" and a swallow's blast radius is a display artifact, never a
write. `find_debug_only_swallows` is non-recursive per directory
(matching the original `executor/*.py` shape), so each top-level source
directory -- and each nested subpackage with its own `*.py` files -- is
listed separately. `tests/` itself is intentionally excluded: it is test
code, not the shipped surface this guard protects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nousergon_lib.testing.debug_swallow_guard import (
    check_against_allowlist,
    check_allowlist_entries_self_contained,
    find_debug_only_swallows,
    load_allowlist,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWLIST_PATH = _REPO_ROOT / ".debug-swallow-allowlist.yaml"

_SOURCE_DIRS = [
    _REPO_ROOT,
    _REPO_ROOT / "charts",
    _REPO_ROOT / "components",
    _REPO_ROOT / "dash_api",
    _REPO_ROOT / "infrastructure",
    _REPO_ROOT / "live",
    _REPO_ROOT / "live" / "charts",
    _REPO_ROOT / "live" / "loaders",
    _REPO_ROOT / "live" / "pages",
    _REPO_ROOT / "loaders",
    _REPO_ROOT / "results",
    _REPO_ROOT / "shared",
    _REPO_ROOT / "views",
]


def _all_swallow_sites() -> dict[str, set[int]]:
    merged: dict[str, set[int]] = {}
    for source_dir in _SOURCE_DIRS:
        if not source_dir.is_dir():
            continue
        for path, lines in find_debug_only_swallows(source_dir, repo_root=_REPO_ROOT).items():
            if lines:
                merged.setdefault(path, set()).update(lines)
    return merged


def test_no_new_debug_only_swallows_outside_allowlist():
    """Every debug-only-or-pass `except Exception` swallow in this repo's
    core source directories is either fixed (raised, or recorded at
    WARNING/ERROR+) or has a non-expired, matching entry in
    `.debug-swallow-allowlist.yaml`."""
    live_sites = _all_swallow_sites()
    allowlist = load_allowlist(_ALLOWLIST_PATH)
    failures = check_against_allowlist(live_sites, allowlist)
    assert not failures, "\n".join(failures)


def test_allowlist_entries_are_self_contained():
    """Every entry names a reason, an expiry, and a tracking issue -- a
    swallow with no named recording surface is not a swallow, it is a
    deletion (alpha-engine-config-I10226)."""
    failures = check_allowlist_entries_self_contained(load_allowlist(_ALLOWLIST_PATH))
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
