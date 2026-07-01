"""
tests/test_accuracy_data_prep.py — Unit tests for accuracy chart data preparation.

Tests the prepare_bucket_data() function from charts/accuracy_chart.py.
Pure data transformation — no Plotly or Streamlit dependencies.
"""

import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from charts.accuracy_chart import prepare_bucket_data, _wilson_ci, SCORE_BUCKET_LABELS


class TestWilsonCI:
    """Tests for _wilson_ci()."""

    def test_zero_total(self):
        lo, hi = _wilson_ci(0, 0)
        assert lo == 0.0
        assert hi == 0.0

    def test_all_successes(self):
        lo, hi = _wilson_ci(100, 100)
        assert lo > 0.9
        assert hi > 0.99

    def test_no_successes(self):
        lo, hi = _wilson_ci(0, 100)
        assert lo == 0.0
        assert hi < 0.1

    def test_half_successes(self):
        lo, hi = _wilson_ci(50, 100)
        assert 0.35 < lo < 0.5
        assert 0.5 < hi < 0.65

    def test_returns_within_bounds(self):
        for s in range(0, 101, 10):
            lo, hi = _wilson_ci(s, 100)
            assert 0.0 <= lo <= hi <= 1.0


class TestPrepareBucketData:
    """Tests for prepare_bucket_data()."""

    def _make_perf_df(self, n=100):
        """Create a synthetic performance DataFrame."""
        rng = np.random.default_rng(42)
        scores = rng.uniform(60, 100, size=n)
        return pd.DataFrame({
            "composite_score": scores,
            "beat_spy_21d": rng.choice([0, 1], size=n, p=[0.4, 0.6]),
        })

    def test_returns_dataframe_with_expected_columns(self):
        df = self._make_perf_df()
        result = prepare_bucket_data(df)

        assert result is not None
        expected_cols = {"bucket", "acc_21d", "count",
                         "ci_21d_lower", "ci_21d_upper"}
        assert expected_cols.issubset(set(result.columns))

    def test_bucket_labels(self):
        df = self._make_perf_df(200)
        result = prepare_bucket_data(df)

        buckets = result["bucket"].astype(str).tolist()
        for b in buckets:
            assert b in SCORE_BUCKET_LABELS

    def test_accuracy_in_percentage_range(self):
        df = self._make_perf_df()
        result = prepare_bucket_data(df)

        assert (result["acc_21d"] >= 0).all()
        assert (result["acc_21d"] <= 100).all()

    def test_count_sums_to_total(self):
        """Bucket counts should sum to the number of rows with scores in [60, 101)."""
        df = self._make_perf_df(100)
        result = prepare_bucket_data(df)

        in_range = (df["composite_score"] >= 60).sum()
        assert result["count"].sum() == in_range

    def test_none_input_returns_none(self):
        assert prepare_bucket_data(None) is None

    def test_empty_df_returns_none(self):
        assert prepare_bucket_data(pd.DataFrame()) is None

    def test_missing_score_column_returns_none(self):
        df = pd.DataFrame({"beat_spy_21d": [1, 0]})
        assert prepare_bucket_data(df) is None

    def test_score_column_fallback(self):
        """Should use 'score' column when 'composite_score' is absent."""
        df = pd.DataFrame({
            "score": [65, 75, 85, 95],
            "beat_spy_21d": [1, 1, 0, 1],
        })
        result = prepare_bucket_data(df)

        assert result is not None
        assert len(result) == 4

    def test_ci_columns_are_non_negative(self):
        df = self._make_perf_df(200)
        result = prepare_bucket_data(df)

        assert (result["ci_21d_lower"] >= -1).all()
        assert (result["ci_21d_upper"] >= -1).all()

    def test_single_bucket(self):
        """All scores in one bucket should produce a single-row result."""
        df = pd.DataFrame({
            "composite_score": [65, 66, 67, 68],
            "beat_spy_21d": [1, 0, 1, 1],
        })
        result = prepare_bucket_data(df)

        assert len(result) == 1
        assert str(result.iloc[0]["bucket"]) == "60-70"
        assert result.iloc[0]["count"] == 4
