"""Tests for the holdings-correlation data-shaping (``shared.correlation``).

Like ``shared.attribution`` (which it builds on), ``shared.correlation`` has no
Streamlit / S3 dependency — pure pandas transforms over the per-(date, ticker)
daily-return long frame — so these run without any mock. Covers: the
(date × ticker) return pivot with current-holdings restriction, pairwise
correlation with the min-overlap gate, and the >threshold high-correlation pair
extraction (config#953).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.attribution import COL_RETURN  # noqa: E402
from shared.correlation import (  # noqa: E402
    correlation_matrix,
    high_correlation_pairs,
    holdings_return_matrix,
)


def _long(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Build a minimal attribution long frame from (date, ticker, return) rows."""
    return pd.DataFrame(
        [{"date": d, "ticker": t, COL_RETURN: r} for d, t, r in rows]
    )


@pytest.fixture
def correlated_long():
    """20 trading days where AAPL and MSFT move almost identically (ρ≈1) and
    GOOGL moves opposite to both (ρ≈-1). One extra name (TSLA) is held for only
    3 days — below the overlap gate."""
    base = np.linspace(-2.0, 2.0, 20)
    rows: list[tuple[str, str, float]] = []
    for i in range(20):
        d = f"2026-05-{i + 1:02d}"
        rows.append((d, "AAPL", float(base[i])))
        rows.append((d, "MSFT", float(base[i] + 0.01 * ((-1) ** i))))  # ~identical
        rows.append((d, "GOOGL", float(-base[i])))                      # inverse
        if i < 3:
            rows.append((d, "TSLA", float(base[i] * 0.5)))
    return _long(rows)


def test_return_matrix_pivots_and_restricts_to_holdings(correlated_long):
    mat = holdings_return_matrix(correlated_long, ["AAPL", "MSFT", "GOOGL"])
    assert list(mat.columns) == ["AAPL", "MSFT", "GOOGL"]  # requested order
    assert len(mat) == 20  # one row per date
    assert mat.index.is_monotonic_increasing  # chronological


def test_return_matrix_drops_unrequested_and_unknown_tickers(correlated_long):
    # TSLA is in the data but not requested; ZZZZ requested but absent.
    mat = holdings_return_matrix(correlated_long, ["AAPL", "ZZZZ"])
    assert list(mat.columns) == ["AAPL"]


def test_return_matrix_empty_inputs():
    assert holdings_return_matrix(None).empty
    assert holdings_return_matrix(pd.DataFrame()).empty
    assert holdings_return_matrix(_long([]), ["AAPL"]).empty


def test_correlation_signs(correlated_long):
    mat = holdings_return_matrix(correlated_long, ["AAPL", "MSFT", "GOOGL"])
    corr = correlation_matrix(mat, min_overlap=5)
    assert set(corr.columns) == {"AAPL", "MSFT", "GOOGL"}
    assert corr.loc["AAPL", "MSFT"] > 0.99      # near-identical
    assert corr.loc["AAPL", "GOOGL"] < -0.99    # inverse
    # Diagonal is self-correlation.
    assert corr.loc["AAPL", "AAPL"] == pytest.approx(1.0)


def test_min_overlap_drops_thin_pairs(correlated_long):
    # TSLA only overlaps 3 days; with a 5-day gate it has no qualifying pair and
    # is dropped entirely from the matrix.
    mat = holdings_return_matrix(
        correlated_long, ["AAPL", "MSFT", "GOOGL", "TSLA"]
    )
    corr = correlation_matrix(mat, min_overlap=5)
    assert "TSLA" not in corr.columns
    assert set(corr.columns) == {"AAPL", "MSFT", "GOOGL"}


def test_correlation_needs_two_tickers():
    mat = holdings_return_matrix(
        _long([("2026-05-01", "AAPL", 1.0), ("2026-05-02", "AAPL", 2.0)]),
        ["AAPL"],
    )
    assert correlation_matrix(mat).empty


def test_high_correlation_pairs_threshold_and_order(correlated_long):
    mat = holdings_return_matrix(correlated_long, ["AAPL", "MSFT", "GOOGL"])
    corr = correlation_matrix(mat, min_overlap=5)
    pairs = high_correlation_pairs(corr, threshold=0.8)
    # Both the +1 (AAPL/MSFT) and the -1 (AAPL/GOOGL, MSFT/GOOGL) pairs qualify
    # on absolute value; each unordered pair appears once.
    keys = {frozenset((a, b)) for a, b, _ in pairs}
    assert frozenset(("AAPL", "MSFT")) in keys
    assert frozenset(("AAPL", "GOOGL")) in keys
    assert len(pairs) == 3
    # Sorted by descending |correlation|.
    mags = [abs(c) for *_xy, c in pairs]
    assert mags == sorted(mags, reverse=True)


def test_high_correlation_pairs_empty_when_below_threshold():
    # Two uncorrelated random-ish series.
    rng = np.random.default_rng(0)
    rows = []
    for i in range(40):
        d = f"2026-05-{i + 1:02d}" if i < 31 else f"2026-06-{i - 30:02d}"
        rows.append((d, "AAA", float(rng.standard_normal())))
        rows.append((d, "BBB", float(rng.standard_normal())))
    corr = correlation_matrix(holdings_return_matrix(_long(rows), ["AAA", "BBB"]))
    assert high_correlation_pairs(corr, threshold=0.8) == []


def test_high_correlation_pairs_empty_matrix():
    assert high_correlation_pairs(pd.DataFrame()) == []
    assert high_correlation_pairs(None) == []
