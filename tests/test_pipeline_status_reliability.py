"""Page-25 cycle-reliability strip (alpha-engine-config-I6919).

The strip answers *"are we making progress or looping?"*, which the existing
red/green surfaces cannot. These tests hold the two properties that make the
answer trustworthy rather than merely present:

1. **Three verdict states, not two.** "No settled cycle" is not "not looping".
2. **No stale fallback.** Every other loader in this module falls back to the
   S3 last-good cache on error. This one must not: a cached window would
   answer a progress question with yesterday's verdict and give the reader no
   way to tell.

The stage-order map is also asserted against the LIVE SF definitions — a
misspelled stage name silently ranks nothing, which is exactly the drift that
made the weekday Telegram digest render empty for months
(alpha-engine-config-I6857).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from loaders.pipeline_status_loader import (  # noqa: E402
    RELIABILITY_STAGE_ORDER,
    ReliabilityResult,
    read_reliability_with_fallback,
)

_ARN = "arn:aws:states:us-east-1:711398986525:stateMachine:ne-weekly-freshness-pipeline"


def _cycle(key, **kw):
    row = {
        "cycle_key": key,
        "attempts": 1,
        "first_attempt_succeeded": True,
        "attempts_to_success": 1,
        "settled": True,
        "recovered": False,
        "depth_index": 3,
        "depth_stage": "Scanner",
        "wall_clock_sec": 600.0,
        "new_causes": [],
        "repeat_causes": [],
        "unresolved_attempts": 0,
    }
    row.update(kw)
    return row


# ── The verdict ──────────────────────────────────────────────────────────


def test_clean_streak_counts_only_trailing_first_attempt_successes():
    rel = ReliabilityResult(
        cycles=[
            _cycle("c1", first_attempt_succeeded=False, attempts_to_success=2, recovered=True),
            _cycle("c2"),
            _cycle("c3"),
        ]
    )
    assert rel.clean_streak == 2


def test_a_recovered_cycle_breaks_the_streak():
    """Succeeded-on-rerun is not clean. That distinction is the metric."""
    rel = ReliabilityResult(
        cycles=[_cycle("c1"), _cycle("c2", first_attempt_succeeded=False, attempts_to_success=3)]
    )
    assert rel.clean_streak == 0


def test_skip_only_cycle_neither_extends_nor_breaks_the_streak():
    """alpha-engine-config-I8069: mirrors nousergon_lib.pipeline_status.
    cycles.ReliabilityWindow.clean_streak exactly. A skip_only cycle (every
    attempt reached a declared no-op terminal, e.g. a THU WeeklyRunDaySkip)
    is neither clean nor dirty — the pipeline was correct to do nothing, and
    it says nothing about whether the next real run will be clean. Before
    this fix the local rebuild read row["first_attempt_succeeded"] (None
    for a skip_only cycle, since it never truly "succeeded" a real attempt)
    as falsy and BROKE the streak — bypassing the lib fix entirely."""
    rel = ReliabilityResult(
        cycles=[
            _cycle("c1"),
            _cycle("c2"),
            _cycle(
                "c3",
                skip_only=True,
                first_attempt_succeeded=None,
                attempts_to_success=None,
                depth_index=None,
                depth_stage=None,
            ),
        ]
    )
    # The skip_only cycle is skipped over entirely — the streak still counts
    # the two real clean cycles beneath it, not reset to 0.
    assert rel.clean_streak == 2


def test_skip_only_cycle_is_excluded_from_looping_judgment():
    """Mirrors ReliabilityWindow.looping: a skip_only cycle is not a cycle
    to judge looping against, so the most-recent NON-skip settled cycle
    decides — alpha-engine-config-I8069."""
    rel = ReliabilityResult(
        cycles=[
            _cycle("c1", repeat_causes=["MorningEnrich:Timeout"]),
            _cycle(
                "c2",
                skip_only=True,
                first_attempt_succeeded=None,
                attempts_to_success=None,
                repeat_causes=[],
            ),
        ]
    )
    assert rel.looping is True


def test_looping_is_none_when_no_cycle_has_settled():
    """Not False. Rendering 'unknown' as 'not looping' asserts a verdict the
    data does not support — the same error as rendering absence as green."""
    rel = ReliabilityResult(cycles=[_cycle("c1", settled=False, first_attempt_succeeded=None)])
    assert rel.looping is None


def test_looping_reads_the_most_recent_settled_cycle():
    rel = ReliabilityResult(
        cycles=[
            _cycle("c1", repeat_causes=["MorningEnrich:E"]),
            _cycle("c2"),
            _cycle("c3", settled=False, first_attempt_succeeded=None),
        ]
    )
    assert rel.looping is False, "c2 is the most recent SETTLED cycle, and it is clean"


def test_the_headline_distinguishes_all_three_states():
    from importlib import import_module

    page = import_module("views.25_Pipeline_Status")

    unknown, _ = page._verdict_line(ReliabilityResult(cycles=[_cycle("c", settled=False)]))
    looping, _ = page._verdict_line(
        ReliabilityResult(cycles=[_cycle("c", repeat_causes=["X:E"])])
    )
    clean, _ = page._verdict_line(ReliabilityResult(cycles=[_cycle("c")]))
    peeling, _ = page._verdict_line(
        ReliabilityResult(
            cycles=[_cycle("c", first_attempt_succeeded=False, attempts_to_success=None,
                           new_causes=["X:E"])]
        )
    )
    assert len({unknown, looping, clean, peeling}) == 4, "each state needs its own headline"
    assert "Looping" in looping
    assert "NEW cause" in peeling, "red-but-progressing must not read as failure"


# ── Degradation ──────────────────────────────────────────────────────────


def test_an_error_surfaces_and_does_not_fall_back_to_a_stale_window():
    """Deliberately unlike read_pipeline_state_with_fallback.

    A cached reliability window would answer "are we making progress" with
    yesterday's verdict, and nothing on the surface would say so.
    """
    with patch(
        "loaders.pipeline_status_loader._cached_reliability",
        side_effect=RuntimeError("boom"),
    ):
        rel = read_reliability_with_fallback(_ARN)
    assert rel.cycles == []
    assert rel.error and "boom" in rel.error


def test_unresolved_attempts_are_summed_so_a_weak_verdict_is_visible():
    rel = ReliabilityResult(
        cycles=[_cycle("c1", unresolved_attempts=2), _cycle("c2", unresolved_attempts=1)]
    )
    assert rel.unresolved_attempts == 3


# ── Stage order vs the live definitions ──────────────────────────────────


_SF_FILES = {
    "ne-weekly-freshness-pipeline": "step_function.json",
    "ne-preopen-trading-pipeline": "step_function_daily.json",
    "ne-postclose-trading-pipeline": "step_function_eod.json",
}

# alpha-engine-config-I7605: this previously hardcoded a bare sibling-checkout
# path with no SF_DEFS_DIR override and no CI-hard-fail guard, unlike
# test_pipeline_status_registry_drift.py's identical-purpose walk of the same
# SF definitions — so on every CI runner (no ~/Development/nousergon-data
# checkout) this SILENTLY skipped forever, which is indistinguishable from a
# pass in the summary line everyone reads. Now consults the same SF_DEFS_DIR
# ci.yml already sets for the sibling guard, and hard-fails (not skips) on CI
# when the checkout is missing.
_DATA_INFRA = (
    Path(os.environ["SF_DEFS_DIR"]) / "infrastructure"
    if os.environ.get("SF_DEFS_DIR")
    else Path.home() / "Development" / "nousergon-data" / "infrastructure"
)
_ON_CI = os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _all_state_names(states: dict) -> set[str]:
    """Including states nested in Parallel branches and Map iterators.

    The weekly Scanner lives inside ResearchPredictorParallel; a top-level
    scan would report it missing and this guard would be reversed into a
    source of false findings.
    """
    found = set(states)
    for body in states.values():
        for branch in body.get("Branches") or []:
            found |= _all_state_names(branch.get("States") or {})
        iterator = body.get("Iterator") or body.get("ItemProcessor")
        if iterator:
            found |= _all_state_names(iterator.get("States") or {})
    return found


@pytest.mark.parametrize("sf_name,filename", sorted(_SF_FILES.items()))
def test_every_declared_stage_exists_in_the_live_definition(sf_name: str, filename: str):
    """A misspelled stage name ranks nothing, silently.

    That is exactly how DIGEST_STATE_ORDER came to carry `Parity` and
    `ModelZooRotation` — names no definition had — and order nothing on the
    weekly side for months (alpha-engine-config-I6857).
    """
    path = _DATA_INFRA / filename
    if not path.exists():
        message = (
            f"{path} not present. CI checks the data repo out and sets "
            f"SF_DEFS_DIR (see ci.yml, `test` job); a dev laptop uses "
            f"~/Development/nousergon-data."
        )
        if _ON_CI:
            pytest.fail(
                f"{message} On CI this is a broken guard, not an absent "
                f"layout — skipping here would report a cross-repo "
                f"invariant as satisfied without ever evaluating it."
            )
        pytest.skip(message)
    known = _all_state_names(json.loads(path.read_text())["States"])
    declared = RELIABILITY_STAGE_ORDER[sf_name]
    missing = sorted(s for s in declared if s not in known)
    assert not missing, f"{sf_name}: stage order names states no definition has: {missing}"


def test_every_pipeline_has_a_stage_order():
    """A pipeline with no spine renders every cycle at depth None — the strip
    loses its progress column without saying anything."""
    assert set(RELIABILITY_STAGE_ORDER) == set(_SF_FILES)
    assert all(RELIABILITY_STAGE_ORDER.values()), "an empty spine is not a spine"
