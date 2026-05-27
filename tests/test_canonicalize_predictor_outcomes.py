"""Tests for `loaders.db_loader.canonicalize_predictor_outcomes`.

Guards the legacy↔canonical column coalesce. Reading only `correct_5d` /
`actual_5d_return` silently drops every prediction emitted after the
2026-05-09 21d canonical-alpha cutover, since `alpha-engine-data` stopped
dual-writing legacy columns at that point.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from loaders.db_loader import canonicalize_predictor_outcomes


def test_empty_frame_passthrough():
    out = canonicalize_predictor_outcomes(pd.DataFrame())
    assert out.empty


def test_canonical_only_row_resolves():
    df = pd.DataFrame({
        "symbol": ["AAPL"],
        "prediction_date": ["2026-05-15"],
        "correct": [1],
        "correct_5d": [None],
        "actual_log_alpha": [0.024],
        "actual_5d_return": [None],
    })
    out = canonicalize_predictor_outcomes(df)
    assert out["_resolved"].iloc[0] == 1
    assert out["_realized_alpha"].iloc[0] == pytest.approx(0.024)


def test_legacy_only_row_resolves():
    df = pd.DataFrame({
        "symbol": ["MSFT"],
        "prediction_date": ["2026-04-01"],
        "correct": [None],
        "correct_5d": [0],
        "actual_log_alpha": [None],
        "actual_5d_return": [-0.013],
    })
    out = canonicalize_predictor_outcomes(df)
    assert out["_resolved"].iloc[0] == 0
    assert out["_realized_alpha"].iloc[0] == pytest.approx(-0.013)


def test_canonical_wins_when_both_present():
    df = pd.DataFrame({
        "correct": [1],
        "correct_5d": [0],
        "actual_log_alpha": [0.05],
        "actual_5d_return": [-0.02],
    })
    out = canonicalize_predictor_outcomes(df)
    assert out["_resolved"].iloc[0] == 1
    assert out["_realized_alpha"].iloc[0] == pytest.approx(0.05)


def test_pending_row_stays_unresolved():
    df = pd.DataFrame({
        "correct": [None],
        "correct_5d": [None],
        "actual_log_alpha": [None],
        "actual_5d_return": [None],
    })
    out = canonicalize_predictor_outcomes(df)
    assert pd.isna(out["_resolved"].iloc[0])
    assert pd.isna(out["_realized_alpha"].iloc[0])


def test_mixed_cutover_window_resolves_each_row_independently():
    # Pre-cutover row has only legacy populated; post-cutover row has only
    # canonical populated. Both must resolve.
    df = pd.DataFrame({
        "symbol": ["AAPL", "AAPL"],
        "prediction_date": ["2026-04-01", "2026-05-15"],
        "correct": [None, 1],
        "correct_5d": [1, None],
        "actual_log_alpha": [None, 0.011],
        "actual_5d_return": [0.022, None],
    })
    out = canonicalize_predictor_outcomes(df)
    assert list(out["_resolved"]) == [1, 1]
    assert out["_realized_alpha"].iloc[0] == pytest.approx(0.022)
    assert out["_realized_alpha"].iloc[1] == pytest.approx(0.011)


def test_only_legacy_columns_present():
    df = pd.DataFrame({
        "correct_5d": [1, 0],
        "actual_5d_return": [0.03, -0.01],
    })
    out = canonicalize_predictor_outcomes(df)
    assert list(out["_resolved"]) == [1, 0]
    assert out["_realized_alpha"].iloc[0] == pytest.approx(0.03)


def test_only_canonical_columns_present():
    df = pd.DataFrame({
        "correct": [1, 0],
        "actual_log_alpha": [0.04, -0.02],
    })
    out = canonicalize_predictor_outcomes(df)
    assert list(out["_resolved"]) == [1, 0]
    assert out["_realized_alpha"].iloc[0] == pytest.approx(0.04)


def test_idempotent_on_already_canonicalized():
    df = pd.DataFrame({
        "correct": [1],
        "correct_5d": [None],
        "actual_log_alpha": [0.024],
        "actual_5d_return": [None],
    })
    once = canonicalize_predictor_outcomes(df)
    twice = canonicalize_predictor_outcomes(once)
    assert twice["_resolved"].iloc[0] == 1
    assert twice["_realized_alpha"].iloc[0] == pytest.approx(0.024)
