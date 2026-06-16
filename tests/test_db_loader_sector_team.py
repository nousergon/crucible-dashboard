"""Tests for the Sector Team Review loaders (get_team_candidates /
get_team_inputs). Patches load_research_db to an in-memory SQLite, mirroring
the decision-review / CIO-review loader test pattern.
"""

import sqlite3
from unittest.mock import patch

import pytest

from loaders.db_loader import get_team_candidates, get_team_inputs

DATE = "2026-06-13"


@pytest.fixture
def mock_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE team_candidates (ticker TEXT, eval_date TEXT, team_id TEXT, "
        "quant_rank INTEGER, quant_score REAL, qual_score REAL, team_recommended INTEGER, "
        "rsi_sub_score REAL, macd_sub_score REAL, ma50_sub_score REAL, "
        "ma200_sub_score REAL, momentum_sub_score REAL)"
    )
    conn.execute(
        "CREATE TABLE team_inputs (ticker TEXT, eval_date TEXT, team_id TEXT, "
        "source TEXT, sector TEXT)"
    )

    def tc(t, team, rank, rec):
        conn.execute(
            "INSERT INTO team_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, DATE, team, rank, 70.0, 60.0, rec, 50, 50, 50, 50, 50),
        )

    def ti(t, team, src, sector):
        conn.execute("INSERT INTO team_inputs VALUES (?,?,?,?,?)", (t, DATE, team, src, sector))

    tc("NVDA", "technology", 1, 1)
    tc("MSFT", "technology", 2, 1)
    tc("AMD", "technology", 3, 0)
    tc("LLY", "healthcare", 1, 1)  # other team — must be excluded
    ti("NVDA", "technology", "scanner", "Information Technology")
    ti("AAPL", "technology", "held_population", "Information Technology")
    ti("LLY", "healthcare", "scanner", "Health Care")
    conn.commit()

    with patch("loaders.db_loader.load_research_db", return_value=conn):
        yield conn
    conn.close()


def test_team_candidates_filtered_and_ordered(mock_db):
    df = get_team_candidates(DATE, "technology")
    assert df["ticker"].tolist() == ["NVDA", "MSFT", "AMD"]  # by quant_rank
    assert "LLY" not in set(df["ticker"])                    # other team excluded
    assert int(df[df["ticker"] == "NVDA"].iloc[0]["team_recommended"]) == 1
    assert "rsi_sub_score" in df.columns


def test_team_inputs_filtered(mock_db):
    df = get_team_inputs(DATE, "technology")
    assert set(df["ticker"]) == {"NVDA", "AAPL"}
    assert dict(zip(df["ticker"], df["source"])) == {
        "NVDA": "scanner", "AAPL": "held_population"}


def test_team_inputs_empty_other_team_isolation(mock_db):
    df = get_team_inputs(DATE, "financials")  # no rows for this team
    assert df.empty


def test_team_inputs_graceful_when_table_absent():
    # Pre-v19 DB: no team_inputs table → loader returns empty, never raises.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE team_candidates (ticker TEXT, eval_date TEXT, team_id TEXT)")
    with patch("loaders.db_loader.load_research_db", return_value=conn):
        assert get_team_inputs(DATE, "technology").empty
    conn.close()
