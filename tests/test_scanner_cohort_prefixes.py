"""The Scanner-ablation tab resolves its arms from the BOARD, not a constant
(alpha-engine-config-I9280).

MEASURED 2026-08-29. ``views/46_Experiments.py`` carried:

    _SCANNER_COHORT_PREFIXES = {"momentum_sleeve": "candidates_shadow/momentum_sleeve/"}

``momentum_sleeve`` has been the CHAMPION since the 2026-07-22 ``config#1186``
cutover, so it correctly stopped writing ``candidates_shadow/`` on 2026-08-20 —
the tab has rendered an empty, permanently-stalling cohort series for it since.
The two arms actually running, ``tech_score_gate`` (shadow from 2026-08-21) and
``mom_12_1_sleeve`` (from 2026-08-18), were never rendered at all.

The page's only guard was a source pin asserting the literal string was
present, which is what held the stale list in place: a pin on a constant cannot
tell a correct constant from a stale one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from loaders.cohort_prefixes import challenger_cohort_prefixes  # noqa: E402

_TMPL = "candidates_shadow/{name}/"

# The live 2026-08-28 board, trimmed to the fields the resolver reads.
_LIVE_BOARD = {
    "champion": "momentum_sleeve",
    "specs": [
        {"name": "momentum_sleeve", "kind": "champion"},
        {"name": "tech_score_gate", "kind": "challenger"},
        {"name": "mom_12_1_sleeve", "kind": "challenger"},
    ],
}


def test_the_live_challengers_are_rendered_and_the_champion_is_not():
    """PRE-FIX: RED — the page resolved a single hardcoded prefix for
    ``momentum_sleeve`` and neither challenger."""
    got = challenger_cohort_prefixes(_LIVE_BOARD, _TMPL)
    assert got == {
        "tech_score_gate": "candidates_shadow/tech_score_gate/",
        "mom_12_1_sleeve": "candidates_shadow/mom_12_1_sleeve/",
    }
    assert "momentum_sleeve" not in got, (
        "the champion is scored from the LIVE candidates/ prefix and writes no "
        "shadow — listing it renders a permanently-empty series that reads as a "
        "broken arm"
    )


def test_an_arm_this_repo_has_never_heard_of_is_still_rendered():
    """The property the constant lacked: a promotion or a newly registered arm
    must move this view with no dashboard edit."""
    board = {
        "champion": "some_future_arm",
        "specs": [
            {"name": "some_future_arm", "kind": "champion"},
            {"name": "momentum_sleeve", "kind": "challenger"},
            {"name": "a_brand_new_arm", "kind": "challenger"},
        ],
    }
    got = challenger_cohort_prefixes(board, _TMPL)
    assert set(got) == {"momentum_sleeve", "a_brand_new_arm"}
    assert "some_future_arm" not in got


def test_a_missing_or_unreadable_board_yields_an_honest_empty():
    """No board is not the same as an arm emitting nothing, and the resolver
    must never invent a prefix list from names held locally — that is the
    defect being removed, not a fallback."""
    for board in (None, {}, {"specs": None}, "not a mapping", {"specs": [None, 7]}):
        assert challenger_cohort_prefixes(board, _TMPL) == {}
