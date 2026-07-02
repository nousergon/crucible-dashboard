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
    """Create an in-memory SQLite DB with test tables.

    score_performance keeps its legacy wide outcome columns (NULL here --
    config#1531 re-sources them from score_performance_outcomes instead, see
    below) so schema-shape assertions (`SELECT *`) still resemble production.
    """
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
        CREATE TABLE score_performance_outcomes (
            id INTEGER PRIMARY KEY, signal_id TEXT NOT NULL,
            symbol TEXT NOT NULL, score_date TEXT NOT NULL,
            horizon_days INTEGER NOT NULL, beat_spy INTEGER,
            stock_return REAL, spy_return REAL, log_alpha REAL,
            is_primary INTEGER NOT NULL, resolved_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(signal_id, horizon_days)
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

    # Seed data. score_performance's legacy outcome columns stay NULL --
    # get_score_performance / get_score_history re-source them from
    # score_performance_outcomes (below) via loaders.outcome_store.
    conn.executemany(
        "INSERT INTO score_performance VALUES (?,?,?,?,?,?)",
        [
            ("AAPL", "2026-04-01", 82, None, None, None),
            ("MSFT", "2026-04-01", 75, None, None, None),
            ("AAPL", "2026-04-08", 85, None, None, None),
        ],
    )
    # Long-format outcomes: decimal units, primary(21d) carries log_alpha.
    # AAPL 2026-04-01: beat=1, stock=0.03, spy=0.01 -> wide 3.0 / 1.0 / beat_spy_21d=1
    # MSFT 2026-04-01: beat=0, stock=-0.01, spy=0.01 -> wide -1.0 / 1.0 / beat_spy_21d=0
    # AAPL 2026-04-08: beat=1, stock=0.04, spy=0.01 -> wide 4.0 / 1.0 / beat_spy_21d=1
    conn.executemany(
        "INSERT INTO score_performance_outcomes "
        "(signal_id, symbol, score_date, horizon_days, beat_spy, "
        " stock_return, spy_return, log_alpha, is_primary, resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("AAPL:2026-04-01", "AAPL", "2026-04-01", 21, 1, 0.03, 0.01, 0.0198, 1, "2026-04-15T00:00:00+00:00"),
            ("MSFT:2026-04-01", "MSFT", "2026-04-01", 21, 0, -0.01, 0.01, -0.0201, 1, "2026-04-15T00:00:00+00:00"),
            ("AAPL:2026-04-08", "AAPL", "2026-04-08", 21, 1, 0.04, 0.01, 0.0296, 1, "2026-04-22T00:00:00+00:00"),
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
