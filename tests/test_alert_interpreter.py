#!/usr/bin/env python3
"""Alerts publish through the DECLARED krepis venv, not through whichever
checkout happened to carry a krepis.

alpha-engine-config-I7168.

**Measured 2026-08-13, after the pinned venv had already been converged to
krepis 0.59.0:**

    /opt/nousergon/krepis-venv        0.59.0   _escape_markdown("/a_b") -> '/a\\_b'
    alpha-engine-dashboard/.venv      0.54.0                            -> '/a-b'

The second is what every alert on this box was published through — six call
sites, all resolving krepis via ``crucible-dashboard/requirements.txt``, a file
whose owners have no reason to think about alert delivery. So the fix for an
alert defect could land, publish, and be installed on the box, and the alerting
path would still carry the defect.

The defect it carried: ``_escape_markdown`` SUBSTITUTED markdown characters
instead of escaping them, and a box-health WARNING named
``/home/ec2-user/flow-doctor/flow-doctor.db`` — a path that does not exist,
while two OTHER files on this box genuinely are named ``flow-doctor.db``.

This is the failure ``krepis-venv/pin.txt`` was created to end for the spot
launchers (I6931), one consumer along. These tests hold the resolution, not the
instance.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
HELPER = INFRA / "alert_py.sh"

#: Every script that publishes an alert from this box.
_PUBLISHERS = [
    "box_health.sh",
    "alert_on_failure.sh",
    "reboot_if_needed.sh",
    "morning-signal-watchdog.sh",
    "boot-pull.sh",
    "deploy-on-merge.sh",
]


def _text(name: str) -> str:
    return (INFRA / name).read_text(encoding="utf-8")


def test_the_publisher_list_matches_what_actually_publishes():
    """A hand list goes stale the first time someone adds a seventh alert.
    This fails when a script publishes an alert and is not covered below."""
    publishing = {
        p.name
        for p in INFRA.glob("*.sh")
        # The helper itself carries the invocation in its usage example.
        if p != HELPER and "-m krepis.alerts publish" in p.read_text(encoding="utf-8")
    }
    assert publishing == set(_PUBLISHERS), (
        "alert publishers changed: "
        f"{sorted(publishing ^ set(_PUBLISHERS))}"
    )


@pytest.mark.parametrize("name", _PUBLISHERS)
def test_every_publisher_resolves_through_the_shared_helper(name: str):
    text = _text(name)
    assert "alert_py.sh" in text, f"{name} does not source the shared resolution"
    assert '"$ALERT_PY" -m krepis.alerts publish' in text, (
        f"{name} publishes on an interpreter other than $ALERT_PY"
    )


@pytest.mark.parametrize("name", _PUBLISHERS)
def test_no_publisher_still_hardcodes_the_dashboard_venv_for_alerts(name: str):
    """The literal that was wrong in all six places."""
    text = _text(name)
    for bad in ('"$VENV_PY" -m krepis.alerts', '"$DASH_PY" -m krepis.alerts'):
        assert bad not in text, f"{name} still publishes via {bad}"


def test_the_helper_prefers_the_declared_venv():
    assert "/opt/nousergon/krepis-venv" in HELPER.read_text(encoding="utf-8")


def test_the_helper_probes_the_import_not_only_the_path():
    """A venv directory that exists with a broken install fails at publish
    time — the moment there is something to report, and the worst moment to
    find out the reporter is broken."""
    assert "import krepis.alerts" in HELPER.read_text(encoding="utf-8")


def test_the_fallback_is_documented_as_a_deliberate_degrade():
    """Fail-loud is the default and this is the carve-out: the drift check's
    own FINDING that the venv is missing is delivered through this path, so
    failing here would silence the report of the condition."""
    text = HELPER.read_text(encoding="utf-8")
    assert "circular" in text.lower()
    assert "stderr" in text.lower(), "the recording surface must be named"


# ── Behaviour, not only shape ───────────────────────────────────────────────


def _run(alert_py: str, krepis_venv: Path | None) -> tuple[str, str]:
    script = textwrap.dedent(
        f"""
        set -uo pipefail
        . "{HELPER}"
        echo "$ALERT_PY"
        """
    )
    child = dict(os.environ)
    child["ALERT_PY"] = alert_py
    if krepis_venv is not None:
        child["KREPIS_VENV"] = str(krepis_venv)
    proc = subprocess.run(  # noqa: S603
        ["bash", "-c", script], capture_output=True, text=True, env=child,
    )
    return proc.stdout.strip(), proc.stderr.strip()


def _stub_venv(root: Path, exit_code: int) -> Path:
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    py.chmod(0o755)
    return venv


def test_a_usable_declared_venv_is_chosen(tmp_path: Path):
    venv = _stub_venv(tmp_path, 0)
    out, err = _run("", venv)
    assert out == str(venv / "bin" / "python")
    assert err == ""


def test_an_absent_declared_venv_falls_back_and_says_so(tmp_path: Path):
    out, err = _run("", tmp_path / "does-not-exist")
    assert out.endswith("alpha-engine-dashboard/.venv/bin/python")
    assert "config-I7168" in err, "the degrade must name its tracking issue"
    assert "unpinned krepis" in err


def test_a_declared_venv_that_cannot_import_krepis_falls_back(tmp_path: Path):
    """Existence is not usability — the case a `-x` check alone passes and the
    publish then fails on."""
    venv = _stub_venv(tmp_path, 1)
    out, err = _run("", venv)
    assert out.endswith("alpha-engine-dashboard/.venv/bin/python")
    assert "unusable" in err


def test_an_explicit_caller_override_wins():
    """So a test, a rescue path, or a future consumer can redirect delivery
    without editing six scripts."""
    out, err = _run("/usr/bin/python3", None)
    assert out == "/usr/bin/python3"
    assert err == ""
