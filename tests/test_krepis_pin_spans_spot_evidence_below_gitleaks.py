"""The krepis pin must carry `spot_evidence` and must stay below `gitleaks`.

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

**Ceiling — below 0.59.15, where ``krepis.session_dlp`` starts requiring a
``gitleaks`` binary and FAILS CLOSED without it** (`alpha-engine-config-I7634`).
The shared box has no gitleaks and ``deploy-on-merge.sh`` installs straight from
this lockfile, so a bump past the ceiling arms a DLP hook the box cannot
satisfy. It surfaces as an LLM failure naming a scanner, not as a pin problem,
which is why a human reading the failure would not look here.

**The ceiling is removable; the floor is not.** When I7634 provisions gitleaks
8.30.1 on the box *and* adds it to the provisioning the box is rebuilt from,
delete the ceiling assertion and the ``<`` bound together. Until then a bump is
a live outage with a misleading error message.
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

#: First krepis release whose ``src/krepis/session_dlp.py`` references gitleaks.
#: Verified by file: ``git grep -l gitleaks v0.59.14 -- src/krepis/`` is empty;
#: at v0.59.15 it returns ``src/krepis/session_dlp.py``.
GITLEAKS_CEILING = (0, 59, 15)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _pinned_lockfile_version() -> tuple[int, ...]:
    lock = (_ROOT / "requirements.txt").read_text()
    m = re.search(r"^krepis\[[^\]]*\]==([0-9][0-9.]*)\s*$", lock, re.MULTILINE)
    assert m, "no pinned krepis== line found in requirements.txt"
    return _version(m.group(1))


def _declared_bounds() -> tuple[tuple[int, ...], tuple[int, ...]]:
    src = (_ROOT / "requirements.in").read_text()
    m = re.search(
        r"^krepis\[[^\]]*\]>=([0-9][0-9.]*),<([0-9][0-9.]*)\s*$", src, re.MULTILINE
    )
    assert m, (
        "requirements.in must declare krepis with BOTH bounds — the floor "
        "carries spot_evidence (I7609/I7675) and the ceiling keeps the "
        "gitleaks-fail-closed DLP hook off a box without gitleaks (I7634)"
    )
    return _version(m.group(1)), _version(m.group(2))


def test_lockfile_pin_carries_spot_evidence() -> None:
    """A resolved krepis below the floor silently disarms spot evidence."""
    assert _pinned_lockfile_version() >= SPOT_EVIDENCE_FLOOR


def test_lockfile_pin_stays_below_the_gitleaks_ceiling() -> None:
    """A resolved krepis at or above the ceiling fails closed on this box."""
    assert _pinned_lockfile_version() < GITLEAKS_CEILING


def test_declared_floor_is_at_or_above_the_spot_evidence_release() -> None:
    floor, _ = _declared_bounds()
    assert floor >= SPOT_EVIDENCE_FLOOR


def test_declared_ceiling_is_at_or_below_the_gitleaks_release() -> None:
    _, ceiling = _declared_bounds()
    assert ceiling <= GITLEAKS_CEILING


def test_lockfile_pin_satisfies_the_declared_bounds() -> None:
    """The lockfile is generated, so this catches a hand-edit of one side."""
    floor, ceiling = _declared_bounds()
    pinned = _pinned_lockfile_version()
    assert floor <= pinned < ceiling


# ── the ceiling must also be enforced UPSTREAM of the lockfile ──────────────
#
# Every assertion above fires only once Dependabot has already opened the PR.
# That is a detector, not a guard: `requirements.in` carries the constraint,
# but Dependabot resolves against `requirements.txt` and never reads the
# `.in`, so it proposed krepis 0.59.25 in the weekly minor-and-patch group on
# 2026-08-21 (#722) and reddened the five safe bumps riding along with it.
# Left alone it re-proposes the same unsafe bump every week forever.
#
# `.github/dependabot.yml` now carries the ceiling as an `ignore`. These tests
# keep that entry in LOCKSTEP with `GITLEAKS_CEILING` above, so the ignore
# cannot be the kind of rule that is enforced only by its own comment: raising
# the ceiling in one place and not the other fails here rather than silently
# re-arming the weekly proposal.


def _dependabot_pip_ignore() -> list[dict]:
    cfg = yaml.safe_load((_ROOT / ".github/dependabot.yml").read_text())
    pip = [u for u in cfg["updates"]
           if u["package-ecosystem"] == "pip" and u["directory"] == "/"]
    assert len(pip) == 1, "expected exactly one root pip update config"
    return pip[0].get("ignore", [])


def test_dependabot_ignores_krepis_at_and_above_the_ceiling() -> None:
    entries = [e for e in _dependabot_pip_ignore()
               if e.get("dependency-name") == "krepis"]
    assert entries, (
        "dependabot.yml must ignore krepis at/above the gitleaks ceiling — "
        "requirements.in's bound does not reach Dependabot, so without this "
        "the unsafe bump is re-proposed every week (I7634)"
    )
    assert len(entries) == 1, "one krepis ignore entry, not several to reconcile"
    assert entries[0].get("versions") == [f">={'.'.join(map(str, GITLEAKS_CEILING))}"], (
        "the dependabot ignore bound must equal GITLEAKS_CEILING exactly — a "
        "ceiling raised in one place and not the other re-arms the proposal"
    )


def test_dependabot_ignore_does_not_reach_below_the_ceiling() -> None:
    """The floor is not removable and the ignore must not block reaching it.

    Versions between SPOT_EVIDENCE_FLOOR and GITLEAKS_CEILING must still be
    proposed: an over-broad ignore would freeze this repo below the release
    that carries `krepis.spot_evidence` (I7609/I7675) and nothing would say so.
    """
    entries = [e for e in _dependabot_pip_ignore()
               if e.get("dependency-name") == "krepis"]
    bound = _version(entries[0]["versions"][0].lstrip(">="))
    assert bound > SPOT_EVIDENCE_FLOOR
