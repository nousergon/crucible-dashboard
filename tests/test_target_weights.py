"""Unit tests for shared.target_weights.build_target_weight_matrix.

Covers the Ticker × trading-day pivot of optimizer target weights that
the order-book rationale page renders as a time-series matrix: the
held_only (option A) vs full-universe (option B) row filter, NaN-vs-0.0
cell semantics, oldest→newest column order, latest-day row sort, and
same-day re-run dedup.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.target_weights import build_target_weight_matrix  # noqa: E402


def _rec(
    ticker: str,
    *,
    tgt_w: float | None = None,
    held: bool = False,
    state: str = "no_action",
) -> dict:
    opt = {}
    if tgt_w is not None:
        opt["target_weight"] = tgt_w
    return {
        "ticker": ticker,
        "held": held,
        "terminal_state": state,
        "optimizer": opt,
    }


def _payload(day: str, records: list[dict], *, run_id: str | None = None) -> dict:
    return {
        "trading_day": day,
        "run_id": run_id or f"{day}-run",
        "tickers": records,
    }


# ── empty / degenerate ─────────────────────────────────────────────────


def test_empty_history_returns_empty():
    assert build_target_weight_matrix([]).empty


def test_no_kept_tickers_returns_empty():
    # Held_only with nothing held / entering → empty.
    hist = [_payload("2026-06-01", [_rec("AAPL", tgt_w=0.04, state="held")])]
    # AAPL not held and not approved_entry → excluded under held_only.
    assert build_target_weight_matrix(hist, held_only=True).empty


# ── option A: held + approved entries ──────────────────────────────────


def test_held_only_keeps_book_and_forming_excludes_rest():
    hist = [
        _payload(
            "2026-06-01",
            [
                _rec("AAPL", tgt_w=0.05, held=True, state="held"),
                _rec("MSFT", tgt_w=0.04, state="approved_entry"),
                _rec("NVDA", tgt_w=0.03, state="predictor_vetoed"),
                _rec("TSLA", tgt_w=0.0, state="no_action_optimizer_zero_weight"),
            ],
        )
    ]
    df = build_target_weight_matrix(hist, held_only=True)
    assert set(df.index) == {"AAPL", "MSFT"}  # NVDA/TSLA excluded


def test_held_membership_is_union_across_window():
    # AAPL absent on day 1, held with a target on day 2 → still a row,
    # day-1 cell NaN (membership is a union across the window; the cell
    # reflects only whether a target existed that day).
    hist = [
        _payload("2026-06-01", [_rec("MSFT", tgt_w=0.03, held=True, state="held")]),
        _payload("2026-06-02", [_rec("AAPL", tgt_w=0.05, held=True, state="held")]),
    ]
    df = build_target_weight_matrix(hist, held_only=True)
    assert set(df.index) == {"AAPL", "MSFT"}
    assert math.isnan(df.loc["AAPL", "2026-06-01"])  # absent that day
    assert df.loc["AAPL", "2026-06-02"] == 5.0


# ── option B: full considered universe ─────────────────────────────────


def test_full_universe_keeps_every_targeted_ticker():
    hist = [
        _payload(
            "2026-06-01",
            [
                _rec("AAPL", tgt_w=0.05, held=True, state="held"),
                _rec("NVDA", tgt_w=0.03, state="predictor_vetoed"),
            ],
        )
    ]
    df = build_target_weight_matrix(hist, held_only=False)
    assert set(df.index) == {"AAPL", "NVDA"}  # vetoed name still shown


# ── cell semantics: NaN (absent) vs 0.0 (deliberate) ───────────────────


def test_zero_target_distinct_from_absent():
    hist = [
        _payload(
            "2026-06-01",
            [_rec("AAPL", tgt_w=0.0, held=True, state="held")],
        ),
        _payload(
            "2026-06-02",
            [
                _rec("AAPL", tgt_w=0.04, held=True, state="held"),
                _rec("MSFT", tgt_w=0.03, state="approved_entry"),
            ],
        ),
    ]
    df = build_target_weight_matrix(hist, held_only=True)
    # AAPL day-1 real 0.0 target → 0.0, not NaN.
    assert df.loc["AAPL", "2026-06-01"] == 0.0
    # MSFT absent on day 1 → NaN.
    assert math.isnan(df.loc["MSFT", "2026-06-01"])


def test_record_with_no_optimizer_target_is_nan_not_kept_in_b():
    # A held ticker with no target at all on the only day: kept under A
    # (held), NaN cell; not kept under B (no target anywhere).
    hist = [_payload("2026-06-01", [_rec("AAPL", held=True, state="held")])]
    df_a = build_target_weight_matrix(hist, held_only=True)
    assert list(df_a.index) == ["AAPL"]
    assert math.isnan(df_a.loc["AAPL", "2026-06-01"])
    assert build_target_weight_matrix(hist, held_only=False).empty


# ── column order + row sort ────────────────────────────────────────────


def test_columns_oldest_to_newest():
    hist = [
        _payload("2026-06-03", [_rec("AAPL", tgt_w=0.05, held=True, state="held")]),
        _payload("2026-06-01", [_rec("AAPL", tgt_w=0.04, held=True, state="held")]),
        _payload("2026-06-02", [_rec("AAPL", tgt_w=0.03, held=True, state="held")]),
    ]
    df = build_target_weight_matrix(hist, held_only=True)
    assert list(df.columns) == ["2026-06-01", "2026-06-02", "2026-06-03"]


def test_rows_sorted_by_latest_day_target_desc():
    hist = [
        _payload(
            "2026-06-01",
            [
                _rec("AAPL", tgt_w=0.02, held=True, state="held"),
                _rec("MSFT", tgt_w=0.09, held=True, state="held"),
            ],
        ),
        _payload(
            "2026-06-02",
            [
                _rec("AAPL", tgt_w=0.08, held=True, state="held"),
                _rec("MSFT", tgt_w=0.03, held=True, state="held"),
            ],
        ),
    ]
    df = build_target_weight_matrix(hist, held_only=True)
    # Latest day (06-02): AAPL 8% > MSFT 3% → AAPL on top.
    assert list(df.index) == ["AAPL", "MSFT"]


# ── same-day re-run dedup ──────────────────────────────────────────────


def test_same_day_rerun_latest_run_wins():
    hist = [
        _payload(
            "2026-06-01",
            [_rec("AAPL", tgt_w=0.04, held=True, state="held")],
            run_id="2026-06-01-0900",
        ),
        _payload(
            "2026-06-01",
            [_rec("AAPL", tgt_w=0.06, held=True, state="held")],
            run_id="2026-06-01-1000",
        ),
    ]
    df = build_target_weight_matrix(hist, held_only=True)
    assert list(df.columns) == ["2026-06-01"]  # one column, deduped
    assert df.loc["AAPL", "2026-06-01"] == 6.0  # later run wins
