"""The Krepis pin must carry spot evidence and enabled DLP support.

**Why this pin has two sides, and why neither is cosmetic.**

This repo's ``requirements.txt`` is not only this repo's. The weekly-freshness
spot dispatcher
(``nousergon-data/infrastructure/lambdas/weekly-freshness-spot-dispatcher/index.py``)
builds ``/home/ec2-user/alpha-engine-dashboard/.venv`` on the ephemeral spot by
running ``pip install -r requirements.txt`` against a clone of THIS repo, and
every weekly-SF launcher then resolves that interpreter as ``LIB_PYTHON``
(``nousergon-data/infrastructure/_spot_common.sh``). So the version resolved
here is the version that launches, dispatches and tears down every spot stage of
all three Step Functions pipelines.

**Floor — 0.59.13, where ``krepis.spot_evidence`` first exists**
(`alpha-engine-config-I7609`, `-I7675`). That module is the teardown chokepoint
that copies a FAILED run's S3 staging to ``_spot_evidence/`` *before* deleting
it. Below the floor the import fails and the launcher takes its fallback branch.
Measured live 2026-08-19 on ``i-08d7e8358772b280c`` (DataPhase1 of
``watch-rerun-2026-08-18-3``): ``No module named krepis.spot_evidence``. On
2026-08-15 the same absence let ``PredictorBacktest``'s teardown delete the only
uncapped copy of its own failure output — that cause is permanently
unrecoverable, and the run is the one the weekly pipeline failed on.

**DLP floor — 0.59.28, carrying strict execution-run-date stage coverage**
(`alpha-engine-config-I7634`, `-I8155`).  Krepis 0.59.15+ enables a
fail-closed gitleaks hook.  The shared-box installer is durable in
``nous-ergon-ops-PR764`` and this repo's CI runs a hash-verified benign scanner
preflight, so a ceiling would weaken rather than protect the system.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]

#: First krepis release containing ``src/krepis/spot_evidence.py``.
#: Verified by file rather than changelog:
#: ``git cat-file -e v0.59.13:src/krepis/spot_evidence.py`` succeeds, and the
#: same command at v0.59.12 does not.
SPOT_EVIDENCE_FLOOR = (0, 59, 13)

#: First Krepis release carrying I8155's strict stage-coverage run-date contract.
DLP_REQUIRED_FLOOR = (0, 59, 28)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _pinned_lockfile_version() -> tuple[int, ...]:
    lock = (_ROOT / "requirements.txt").read_text()
    m = re.search(r"^krepis(?:\[[^\]]*\])?==([0-9][0-9.]*)\s*$", lock, re.MULTILINE)
    assert m, "no pinned krepis== line found in requirements.txt"
    return _version(m.group(1))


def _declared_floor() -> tuple[int, ...]:
    src = (_ROOT / "requirements.in").read_text()
    m = re.search(r"^krepis\[[^\]]*\]>=([0-9][0-9.]*)\s*$", src, re.MULTILINE)
    assert m, "requirements.in must declare a floor-only Krepis requirement"
    return _version(m.group(1))


def test_lockfile_pin_carries_spot_evidence() -> None:
    """A resolved krepis below the floor silently disarms spot evidence."""
    assert _pinned_lockfile_version() >= SPOT_EVIDENCE_FLOOR


def test_lockfile_pin_carries_the_dlp_stage_coverage_contract() -> None:
    assert _pinned_lockfile_version() >= DLP_REQUIRED_FLOOR


def test_declared_floor_is_at_or_above_the_spot_evidence_release() -> None:
    assert _declared_floor() >= SPOT_EVIDENCE_FLOOR


def test_declared_floor_carries_the_dlp_stage_coverage_contract() -> None:
    assert _declared_floor() >= DLP_REQUIRED_FLOOR


def test_lockfile_pin_satisfies_the_declared_floor() -> None:
    """The lockfile is generated, so this catches a hand-edit of one side."""
    floor = _declared_floor()
    pinned = _pinned_lockfile_version()
    assert floor <= pinned


def test_nothing_caps_krepis_below_the_dlp_floor() -> None:
    """Krepis must be free to move UP.

    This was originally an assertion about Dependabot's root pip config: it
    had carried an ``ignore`` entry pinning krepis below the gitleaks-enabled
    releases, and removing that entry is what let the DLP floor rise. The pip
    ecosystem itself was removed in alpha-engine-config-I9060 — Dependabot
    edits the compiled ``requirements.txt`` and cannot recompile it from
    ``requirements.in``, so every pip PR it opened failed
    ``lockfile-reproducible`` by construction and krepis never moved by that
    route either.

    The invariant survives the mechanism change, so it is asserted against
    BOTH: no pip entry may re-appear carrying a krepis ignore, and the
    producer that now owns the upgrade must exist. A repo with neither has no
    path from a released krepis to the spot box at all.
    """
    cfg = yaml.safe_load((_ROOT / ".github/dependabot.yml").read_text())
    for update in cfg["updates"]:
        if update.get("package-ecosystem") != "pip":
            continue
        assert not any(entry.get("dependency-name") == "krepis"
                       for entry in update.get("ignore", [])), \
            "a pip Dependabot entry caps krepis below the DLP floor again"

    producer = _ROOT / ".github/upgrade_lock.sh"
    assert producer.exists(), (
        "no pip Dependabot entry AND no .github/upgrade_lock.sh — nothing "
        "upgrades krepis, so the DLP floor can only ever be raised by hand"
    )
    assert "--upgrade" in producer.read_text(), (
        "the producer does not upgrade; krepis would stay wherever the "
        "lockfile last left it"
    )


def test_ci_installs_verified_gitleaks_and_runs_a_no_provider_dlp_preflight() -> None:
    """The guard must prove the enabled scanner works, not cap Krepis below it."""
    ci = (_ROOT / ".github/workflows/ci.yml").read_text()
    for required in (
        'GITLEAKS_VERSION="8.30.1"',
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        "sha256sum --check",
        "gitleaks version",
        "KREPIS_GITLEAKS_DIR",
        "from krepis.session_dlp import DLP_OK, scan_request",
        "assert verdict == DLP_OK",
        "Run tests\n        env:\n          SF_DEFS_DIR:",
        "KREPIS_GITLEAKS_DIR: ${{ github.workspace }}/.ci/gitleaks",
    ):
        assert required in ci, f"CI DLP preflight is missing {required!r}"
