"""Tests for loaders/db_loader.py — query functions with in-memory SQLite.

Patches load_research_db to return an in-memory connection, avoiding S3 deps.
"""

import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from loaders.db_loader import (
    _normalize_score_col,
    get_distinct_symbols,
    get_investment_thesis,
    get_macro_snapshots,
    get_predictor_outcomes,
    get_score_history,
    get_score_performance,
    get_top_recent_symbols,
    query_research_db,
)


@pytest.fixture
def mock_db():
    """Create an in-memory SQLite DB with test tables."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE score_performance (
            symbol TEXT, score_date TEXT, score REAL,
            beat_spy_21d INTEGER,
            return_21d REAL,
            spy_21d_return REAL
        )
    """)
    conn.execute("""
        CREATE TABLE investment_thesis (
            symbol TEXT, date TEXT, score REAL, rating TEXT, thesis_summary TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE macro_snapshots (
            date TEXT, market_regime TEXT, vix REAL, sp500_close REAL
        )
    """)
    conn.execute("""
        CREATE TABLE predictor_outcomes (
            symbol TEXT, prediction_date TEXT, predicted_direction TEXT,
            prediction_confidence REAL, actual_5d_return REAL, correct_5d INTEGER
        )
    """)

    # Seed data
    conn.executemany(
        "INSERT INTO score_performance VALUES (?,?,?,?,?,?)",
        [
            ("AAPL", "2026-04-01", 82, 1, 0.03, 0.01),
            ("MSFT", "2026-04-01", 75, 0, -0.01, 0.01),
            ("AAPL", "2026-04-08", 85, 1, 0.04, 0.01),
        ],
    )
    conn.executemany(
        "INSERT INTO investment_thesis VALUES (?,?,?,?,?)",
        [
            ("AAPL", "2026-04-08", 85, "BUY", "Strong momentum"),
            ("MSFT", "2026-04-08", 75, "HOLD", "Stable"),
            ("AAPL", "2026-04-01", 82, "BUY", "Rising conviction"),
        ],
    )
    conn.executemany(
        "INSERT INTO macro_snapshots VALUES (?,?,?,?)",
        [
            ("2026-04-01", "neutral", 18.5, 5200),
            ("2026-04-08", "bull", 15.2, 5350),
        ],
    )
    conn.executemany(
        "INSERT INTO predictor_outcomes VALUES (?,?,?,?,?,?)",
        [
            ("AAPL", "2026-04-08", "UP", 0.72, 0.03, 1),
            ("MSFT", "2026-04-08", "DOWN", 0.65, -0.02, 1),
        ],
    )
    conn.commit()
    return conn


class TestNormalizeScoreCol:
    def test_renames_score(self):
        df = pd.DataFrame({"score": [80, 75], "symbol": ["A", "B"]})
        result = _normalize_score_col(df)
        assert "composite_score" in result.columns
        assert "score" not in result.columns

    def test_no_rename_if_composite_exists(self):
        df = pd.DataFrame({"score": [80], "composite_score": [82]})
        result = _normalize_score_col(df)
        assert result["composite_score"].iloc[0] == 82

    def test_empty_df(self):
        result = _normalize_score_col(pd.DataFrame())
        assert result.empty


class TestQueryResearchDb:
    def test_query_returns_df(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = query_research_db("SELECT * FROM macro_snapshots")
            assert len(df) == 2

    def test_query_with_params(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = query_research_db("SELECT * FROM investment_thesis WHERE symbol = ?", params=("AAPL",))
            assert len(df) == 2

    def test_query_no_connection(self):
        with patch("loaders.db_loader.load_research_db", return_value=None):
            df = query_research_db("SELECT 1")
            assert df.empty

    def test_query_bad_sql(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = query_research_db("SELECT * FROM nonexistent_table")
            assert df.empty


class TestGetScorePerformance:
    def test_returns_sorted(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_score_performance()
            assert len(df) == 3
            assert df.iloc[0]["score_date"] <= df.iloc[-1]["score_date"]
            assert "composite_score" in df.columns


class TestGetInvestmentThesis:
    def test_all(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_investment_thesis()
            assert len(df) == 3

    def test_filtered(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_investment_thesis("MSFT")
            assert len(df) == 1
            assert df.iloc[0]["symbol"] == "MSFT"


class TestGetMacroSnapshots:
    def test_returns_rows(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_macro_snapshots()
            assert len(df) == 2


class TestGetDistinctSymbols:
    def test_returns_sorted(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            symbols = get_distinct_symbols()
            assert symbols == ["AAPL", "MSFT"]

    def test_no_connection(self):
        with patch("loaders.db_loader.load_research_db", return_value=None):
            symbols = get_distinct_symbols()
            assert symbols == []


class TestGetScoreHistory:
    def test_returns_symbol_rows(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_score_history("AAPL")
            assert len(df) == 2
            assert "composite_score" in df.columns


class TestGetTopRecentSymbols:
    def test_returns_top_n(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_top_recent_symbols(n=2)
            assert len(df) == 2
            # Highest score should be first (AAPL 85 > MSFT 75)
            assert df.iloc[0]["symbol"] == "AAPL"


class TestGetPredictorOutcomes:
    def test_all(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_predictor_outcomes()
            assert len(df) == 2

    def test_filtered(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            df = get_predictor_outcomes("MSFT")
            assert len(df) == 1
            assert df.iloc[0]["predicted_direction"] == "DOWN"
