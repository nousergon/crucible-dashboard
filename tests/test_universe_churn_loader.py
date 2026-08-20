"""Consumer contract tests for loaders/universe_churn.py.

Pins the transforms behind the Universe Churn page against the
``universe_membership`` artifact crucible-research publishes
(``scoring/universe_membership.py``, ``schema_version=1``). Locks:

  1. Series assembly: date-ordered, absent cuts skipped (not zero-filled).
  2. Churn arithmetic: retained/new/dropped vs the PRIOR cycle; first cycle
     has no predecessor and reports None, never 0.
  3. Tenure: weeks-in, current streak counted from the END of the series.
  4. Survivors: intersection across ALL cycles; empty is a real answer.
  5. Heatmap matrix ordering + top_n truncation.
  6. Degradation: empty history, single cycle, a gap in the date series.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.universe_churn import (  # noqa: E402
    CUT_LABELS,
    backfilled_dates,
    churn_table,
    cut_comparison,
    cut_label,
    cut_names,
    membership_matrix,
    membership_series,
    survivors,
    tenure_distribution,
    tenure_table,
)

_CUT = "scanner_champion_60"


def _m(run_date: str, tickers: list[str], *, cut: str = _CUT, **extra) -> dict:
    return {
        "schema_version": 1,
        "run_date": run_date,
        "predictor_universe_cut": _CUT,
        "cuts": {cut: {
            "basis": "scanner_champion_rank",
            "size": len(tickers),
            "tickers": sorted(tickers),
            "source": f"candidates/{run_date}/candidates.json::scanner_tickers",
        }},
        **extra,
    }


def _history() -> list[dict]:
    """Three cycles: A persists throughout, B drops then returns, C/D rotate."""
    return [
        _m("2026-07-10", ["A", "B", "C"]),
        _m("2026-07-17", ["A", "D"]),
        _m("2026-07-24", ["A", "B", "D"]),
    ]


# ── 1. Series assembly ───────────────────────────────────────────────────────

def test_series_is_date_ordered_regardless_of_input_order():
    shuffled = [_m("2026-07-24", ["A"]), _m("2026-07-10", ["B"]), _m("2026-07-17", ["C"])]
    assert [d for d, _ in membership_series(shuffled, _CUT)] == [
        "2026-07-10", "2026-07-17", "2026-07-24",
    ]


def test_artifact_without_the_cut_is_skipped_not_zero_filled():
    # An absent cut means "this cycle's producer didn't emit it", NOT "the cut
    # was empty". Zero-filling would invent 100% churn out of a schema change.
    history = [_m("2026-07-10", ["A"]), _m("2026-07-17", ["X"], cut="some_other_cut")]
    series = membership_series(history, _CUT)
    assert [d for d, _ in series] == ["2026-07-10"]


def test_cut_names_are_discovered_from_the_artifacts():
    history = [_m("2026-07-10", ["A"]), _m("2026-07-17", ["B"], cut="attractiveness_top_25")]
    names = cut_names(history)
    # Known cuts come first in CUT_LABELS order, unknown ones after.
    assert names == ["scanner_champion_60", "attractiveness_top_25"]


def test_the_champion_cut_is_first_and_labelled_as_such():
    """alpha-engine-config-I6786. The view defaults its comparison to the first
    three names and its detail cut to the first, so ordering here decides what
    the page opens on. `attractiveness_top_20` is what the predictor resolves
    its daily universe from (`membership.json::predictor_universe_cut`); it had
    no CUT_LABELS entry, so it sorted into the unknown-cut tail and the page
    opened on three cuts, none of them the traded one."""
    history = [
        _m("2026-07-10", ["A"], cut="attractiveness_top_60"),
        _m("2026-07-10", ["B"], cut="attractiveness_top_20"),
    ]
    assert cut_names(history)[0] == "attractiveness_top_20"
    assert "CHAMPION" in cut_label("attractiveness_top_20")
    assert "challenger" in cut_label("scanner_top_20")


def test_unknown_cut_still_renders_under_its_raw_name():
    assert cut_label("a_new_cut_the_producer_added") == "a_new_cut_the_producer_added"


def test_champion_cut_and_its_deprecated_alias_both_have_friendly_labels():
    """alpha-engine-config-I7818: the champion cut is `scanner_champion_60`;
    `scanner_gate_baseline_60` is still emitted for one deprecation window and
    must not fall into the raw-name unknown-cut tail mid-transition."""
    assert cut_label("scanner_champion_60") != "scanner_champion_60"
    assert cut_label("scanner_gate_baseline_60") != "scanner_gate_baseline_60"


def test_scanner_candidates_is_retired_and_reads_as_an_unknown_cut():
    """The I7578 alias is retired outright (I7818) — no longer emitted by the
    producer, and no longer a known label here. A cycle's ARCHIVED artifact
    that still carries it under the old key degrades to the raw-name tail
    rather than crashing (`cut_names`' documented unknown-cut behavior)."""
    assert "scanner_candidates" not in CUT_LABELS
    assert cut_label("scanner_candidates") == "scanner_candidates"


# ── 2. Churn arithmetic ──────────────────────────────────────────────────────

def test_churn_counts_against_the_prior_cycle():
    table = churn_table(_history(), _CUT).set_index("as_of")
    # 07-17 {A,D} vs 07-10 {A,B,C}: retained A; new D; dropped B, C.
    row = table.loc["2026-07-17"]
    assert (row["retained"], row["new"], row["dropped"]) == (1, 1, 2)
    # 07-24 {A,B,D} vs 07-17 {A,D}: retained A and D; new B (a RETURNING name
    # counts as new — it was not in the immediately prior cut).
    row = table.loc["2026-07-24"]
    assert (row["retained"], row["new"], row["dropped"]) == (2, 1, 0)


def test_retention_pct_is_share_of_the_prior_cut():
    table = churn_table(_history(), _CUT).set_index("as_of")
    assert table.loc["2026-07-17"]["retention_pct"] == 100 * 1 / 3
    assert table.loc["2026-07-24"]["retention_pct"] == 100 * 2 / 2


def test_first_cycle_reports_none_not_zero():
    # 0 retained would read as "nothing carried over"; the truth is "there was
    # nothing to carry over from".
    # (pandas stores the missing value as NaN in a numeric column — the point
    # is that it is MISSING, not that it is the number zero.)
    first = churn_table(_history(), _CUT).iloc[0]
    assert first["size"] == 3
    assert pd.isna(first["retained"])
    assert pd.isna(first["new"])
    assert pd.isna(first["retention_pct"])


def test_churn_uses_membership_not_rank_order():
    # The same names in a different artifact order must be 100% retention.
    history = [_m("2026-07-10", ["A", "B", "C"]), _m("2026-07-17", ["C", "B", "A"])]
    table = churn_table(history, _CUT)
    assert table.iloc[1]["retention_pct"] == 100.0
    assert table.iloc[1]["new"] == 0


# ── 3. Tenure ────────────────────────────────────────────────────────────────

def test_weeks_in_counts_every_appearance():
    tenure = tenure_table(_history(), _CUT).set_index("ticker")
    assert tenure.loc["A"]["weeks_in"] == 3
    assert tenure.loc["B"]["weeks_in"] == 2   # in 07-10 and 07-24, out 07-17
    assert tenure.loc["C"]["weeks_in"] == 1


def test_current_streak_counts_back_from_the_latest_cycle():
    tenure = tenure_table(_history(), _CUT).set_index("ticker")
    assert tenure.loc["A"]["current_streak"] == 3
    # B was in 07-10, OUT 07-17, back in 07-24 → streak is 1, not 2. "How long
    # has it been in right now" ≠ "its longest run ever".
    assert tenure.loc["B"]["current_streak"] == 1
    # C left and never returned → zero current streak despite one appearance.
    assert tenure.loc["C"]["current_streak"] == 0
    assert not tenure.loc["C"]["in_latest"]


def test_tenure_sorted_longest_first():
    assert tenure_table(_history(), _CUT)["ticker"].tolist()[0] == "A"


def test_first_and_last_seen_bracket_appearances():
    tenure = tenure_table(_history(), _CUT).set_index("ticker")
    assert tenure.loc["B"]["first_seen"] == "2026-07-10"
    assert tenure.loc["B"]["last_seen"] == "2026-07-24"


def test_tenure_distribution_counts_names_per_tenure():
    dist = tenure_distribution(_history(), _CUT).set_index("weeks_in")
    assert dist.loc[3]["tickers"] == 1     # A
    assert dist.loc[2]["tickers"] == 2     # B, D
    assert dist.loc[1]["tickers"] == 1     # C


# ── 4. Survivors ─────────────────────────────────────────────────────────────

def test_survivors_are_present_in_every_cycle():
    assert survivors(_history(), _CUT) == ["A"]


def test_no_survivors_is_a_real_answer_not_an_error():
    history = [_m("2026-07-10", ["A"]), _m("2026-07-17", ["B"])]
    assert survivors(history, _CUT) == []


# ── 5. Heatmap matrix ────────────────────────────────────────────────────────

def test_matrix_is_binary_membership_ordered_by_tenure():
    matrix = membership_matrix(_history(), _CUT)
    assert list(matrix.columns) == ["2026-07-10", "2026-07-17", "2026-07-24"]
    assert matrix.index[0] == "A"
    assert matrix.loc["A"].tolist() == [1, 1, 1]
    assert matrix.loc["B"].tolist() == [1, 0, 1]


def test_matrix_top_n_keeps_the_longest_tenured():
    matrix = membership_matrix(_history(), _CUT, top_n=2)
    assert len(matrix.index) == 2
    assert "A" in matrix.index
    assert "C" not in matrix.index   # the one-cycle name is cut first


# ── 6. Degradation ───────────────────────────────────────────────────────────

def test_empty_history_degrades_everywhere():
    assert membership_series([], _CUT) == []
    assert churn_table([], _CUT).empty
    assert tenure_table([], _CUT).empty
    assert membership_matrix([], _CUT).empty
    assert survivors([], _CUT) == []
    assert cut_comparison([], [_CUT]).empty
    assert tenure_distribution([], _CUT).empty


def test_single_cycle_yields_one_row_with_no_comparison():
    history = [_m("2026-07-24", ["A", "B"])]
    table = churn_table(history, _CUT)
    assert len(table) == 1
    assert pd.isna(table.iloc[0]["retained"])
    # Both names trivially "survive" a one-cycle history.
    assert survivors(history, _CUT) == ["A", "B"]


def test_gap_in_the_date_series_compares_adjacent_recorded_cycles():
    # Cycles are compared to the previous RECORDED one, not to a calendar
    # neighbour — a skipped week must not be read as a total wipe.
    history = [_m("2026-05-29", ["A"]), _m("2026-07-24", ["A"])]
    table = churn_table(history, _CUT)
    assert table.iloc[1]["retained"] == 1
    assert table.iloc[1]["retention_pct"] == 100.0


def test_empty_ticker_list_does_not_create_a_phantom_cycle():
    history = [_m("2026-07-10", ["A"]), _m("2026-07-17", [])]
    assert [d for d, _ in membership_series(history, _CUT)] == ["2026-07-10"]


# ── Provenance ───────────────────────────────────────────────────────────────

def test_backfilled_cycles_are_identifiable():
    history = [
        _m("2026-07-10", ["A"], backfilled_from="candidates.json + history parquet"),
        _m("2026-07-24", ["A"]),
    ]
    assert backfilled_dates(history) == ["2026-07-10"]


def test_cut_comparison_summarizes_each_cut():
    row = cut_comparison(_history(), [_CUT]).iloc[0]
    assert row["cut"] == cut_label(_CUT)
    assert row["cycles"] == 3
    assert row["survivors"] == 1
