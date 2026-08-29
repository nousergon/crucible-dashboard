"""Resolve a champion/challenger slot's shadow-cohort prefixes from its BOARD.

Lives here rather than inside ``views/46_Experiments.py`` so it is importable
without a Streamlit runtime and can carry a real behavioural test — the page
module executes S3 reads at import, so anything defined there is testable only
by pinning its source text, and a source pin is what locked the stale arm list
in place for four weeks (alpha-engine-config-I9280).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def challenger_cohort_prefixes(
    board: Any, tmpl: str, *, champion_kind: str = "champion"
) -> dict[str, str]:
    """``{arm_name: shadow_prefix}`` for every CHALLENGER named on ``board``.

    The board already names every registered arm and its ``kind``, so the arm
    list is true by construction and a promotion moves the view with no
    dashboard edit — the property the hardcoded constant this replaces did not
    have (champion-challenger-policy.md §7.5: a view names the arm, never a
    literal that goes stale when the champion changes).

    The champion is excluded deliberately. A champion is scored from the LIVE
    artifact prefix and writes no shadow of its own, so listing it renders a
    permanently-empty cohort series that reads as a broken arm — which is
    exactly what the Scanner tab showed for ``momentum_sleeve`` from
    2026-08-20 onward, after the 2026-07-22 cutover promoted it.

    Returns ``{}`` for a board that is missing, unreadable or carries no
    ``specs``. That is an honest empty, and the caller's own "no leaderboard
    builds yet" path states it — inventing prefixes from names held locally is
    the defect being removed, not a fallback.
    """
    if not isinstance(board, Mapping):
        return {}
    out: dict[str, str] = {}
    for spec in board.get("specs") or []:
        if not isinstance(spec, Mapping):
            continue
        name = spec.get("name")
        if not name or spec.get("kind") == champion_kind:
            continue
        out[str(name)] = tmpl.format(name=name)
    return out
