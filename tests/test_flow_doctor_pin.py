"""Guard test: verify installed flow-doctor matches the compiled lockfile pin.

The issue (alpha-engine-config-I3226): the trading box was stuck at flow-doctor
0.8.1 for months while the code depended on fixes shipped in 0.8.3, 0.8.5, and
0.8.6, because the floating range (flow-doctor>=0.8.1,<0.9 resolved transitively
through krepis) satisfied the already-installed version and boot-pull's plain
``pip install -r requirements.txt`` never re-checked for newer compatible
releases.

The fix: uv-compiled lockfile (requirements.txt) pins flow-doctor to an exact
version. This guard test imports the actual installed package and asserts its
``__version__`` matches the pin in the lockfile, catching any mismatch between
what the lockfile declares and what the venv actually resolved — whether from a
stale deploy, a manual ``pip install`` override, or a lockfile that wasn't
recompiled after a transitive dep bump.

Run::

    pip install -r requirements.txt
    python3 -m pytest tests/test_flow_doctor_pin.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _get_locked_flow_doctor_version() -> str:
    """Extract the exact flow-doctor version pinned in requirements.txt.

    The lockfile (uv-compiled requirements.txt) pins the full transitive
    closure with ``==`` specifiers. We parse the line that names
    ``flow-doctor`` directly (not as a ``# via`` comment).
    """
    text = (REPO_ROOT / "requirements.txt").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        # Match: flow-doctor==X.Y.Z  (the package line, not a "# via" comment)
        match = re.match(r"^flow-doctor==(\d+\.\d+\.\d+)$", stripped)
        if match:
            return match.group(1)
    raise AssertionError(
        "Could not find pinned flow-doctor version in requirements.txt. "
        "The lockfile may not have an explicit flow-doctor entry — run "
        "'uv pip compile requirements.in -o requirements.txt' to resolve it."
    )


def test_flow_doctor_version_matches_pin():
    """The installed flow-doctor version must match the compiled lockfile pin.

    A mismatch means the venv resolved a different version than what the
    lockfile declares — either a stale deploy, a pip override, or a lockfile
    that wasn't recompiled after a transitive dep bump.
    """
    try:
        import flow_doctor
    except ImportError:
        pytest.skip("flow-doctor not installed (test only runs in a full-venv CI context)")

    locked_version = _get_locked_flow_doctor_version()
    installed_version = flow_doctor.__version__

    assert installed_version == locked_version, (
        f"flow-doctor version mismatch: installed={installed_version} "
        f"locked={locked_version}. The venv resolved a different version than "
        f"requirements.txt declares. Run 'pip install -r requirements.txt' "
        f"to reconcile, or 'uv pip compile requirements.in -o requirements.txt' "
        f"to recompile the lockfile."
    )
