"""
tests/test_db_loader.py — Unit tests for loaders/db_loader.py.

Tests the _normalize_score_col() function which renames 'score' to
'composite_score' for backward compatibility with dashboard pages.
No S3 or SQLite connections needed.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# We need to mock streamlit and the S3 loader before importing db_loader,
# since db_loader imports from s3_loader which uses @st.cache decorators.
from unittest.mock import MagicMock

sys.modules.setdefault("streamlit", MagicMock())
sys.modules.setdefault("yaml", __import__("yaml") if "yaml" in sys.modules else MagicMock())

# Mock s3_loader so db_loader can import without config.yaml
mock_s3_loader = MagicMock()
mock_s3_loader.load_config.return_value = {
    "s3": {"research_bucket": "test-bucket"},
    "paths": {"research_db": "research.db"},
}
sys.modules["loaders.s3_loader"] = mock_s3_loader

from loaders.db_loader import _normalize_score_col


# ---------------------------------------------------------------------------
# Tests: _normalize_score_col
# ---------------------------------------------------------------------------

class TestNormalizeScoreCol:
    """Tests for _normalize_score_col()."""

    def test_renames_score_to_composite_score(self):
        """DataFrame with 'score' column should get it renamed to 'composite_score'."""
        df = pd.DataFrame({
            "symbol": ["AAPL", "MSFT"],
            "score": [85.0, 72.0],
            "score_date": ["2024-01-01", "2024-01-01"],
        })
        result = _normalize_score_col(df)

        assert "composite_score" in result.columns
        assert "score" not in result.columns
        assert result["composite_score"].tolist() == [85.0, 72.0]

    def test_already_has_composite_score_unchanged(self):
        """DataFrame already having 'composite_score' should be unchanged."""
        df = pd.DataFrame({
            "symbol": ["AAPL"],
            "composite_score": [90.0],
        })
        result = _normalize_score_col(df)

        assert "composite_score" in result.columns
        assert result["composite_score"].iloc[0] == 90.0

    def test_both_columns_present_no_rename(self):
        """DataFrame with both 'score' and 'composite_score' should keep both."""
        df = pd.DataFrame({
            "symbol": ["AAPL"],
            "score": [85.0],
            "composite_score": [90.0],
        })
        result = _normalize_score_col(df)

        assert "score" in result.columns
        assert "composite_score" in result.columns
        assert result["score"].iloc[0] == 85.0
        assert result["composite_score"].iloc[0] == 90.0

    def test_empty_dataframe_passes_through(self):
        """Empty DataFrame should pass through without error."""
        df = pd.DataFrame()
        result = _normalize_score_col(df)
        assert result.empty

    def test_no_score_columns_unchanged(self):
        """DataFrame without 'score' or 'composite_score' should be unchanged."""
        df = pd.DataFrame({
            "symbol": ["AAPL"],
            "price": [150.0],
        })
        result = _normalize_score_col(df)

        assert "score" not in result.columns
        assert "composite_score" not in result.columns
        assert "price" in result.columns

    def test_does_not_modify_original(self):
        """The function should not modify the original DataFrame in place."""
        df = pd.DataFrame({
            "symbol": ["AAPL"],
            "score": [85.0],
        })
        original_cols = list(df.columns)
        _normalize_score_col(df)

        # Original should still have 'score' (rename returns a new df)
        assert list(df.columns) == original_cols
