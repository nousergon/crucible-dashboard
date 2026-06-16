"""Tests for the CIO-review loaders (get_cio_inputs / get_cio_evaluations).

Patches load_research_db to an in-memory SQLite seeded with the full
team_candidates + cio_evaluations schema the loaders select, mirroring the
existing decision-review test pattern.
"""

import sqlite3
from unittest.mock import patch

import pytest

from loaders.db_loader import get_cio_evaluations, get_cio_inputs

DATE = "2026-05-16"
OTHER = "2026-05-09"


@pytest.fixture
def mock_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE team_candidates (ticker TEXT, eval_date TEXT, team_id TEXT, "
        "quant_rank INTEGER, quant_score REAL, qual_score REAL, team_recommended INTEGER)"
    )
    conn.execute(
        "CREATE TABLE cio_evaluations (ticker TEXT, eval_date TEXT, team_id TEXT, "
        "quant_score REAL, qual_score REAL, combined_score REAL, macro_shift REAL, "
        "final_score REAL, cio_decision TEXT, cio_conviction INTEGER, cio_rank INTEGER, "
        "rationale TEXT, rule_tags TEXT)"
    )

    def tc(t, team, rank, q, ql, rec, date=DATE):
        conn.execute("INSERT INTO team_candidates VALUES (?,?,?,?,?,?,?)",
                     (t, date, team, rank, q, ql, rec))

    def cio(t, team, dec, rank=None, conv=None, fs=None, rat="", tags="[]"):
        conn.execute("INSERT INTO cio_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (t, DATE, team, 50.0, 50.0, 50.0, 0.0, fs, dec, conv, rank, rat, tags))

    # Sector teams: NVDA + MSFT recommended (tech), TSLA recommended (consumer),
    # AMD ranked but NOT recommended. A row on OTHER date must be excluded.
    tc("NVDA", "technology", 1, 85, 70, 1)
    tc("MSFT", "technology", 2, 80, 68, 1)
    tc("AMD", "technology", 3, 60, 50, 0)   # ranked, not recommended
    tc("TSLA", "consumer", 1, 66, 60, 1)
    tc("OLD", "technology", 1, 90, 90, 1, date=OTHER)  # different cycle

    # CIO: NVDA ADVANCE (rank 1), TSLA ADVANCE_FORCED (rank 2), MSFT REJECT (no rank)
    cio("NVDA", "technology", "ADVANCE", rank=1, conv=80, fs=82.0, rat="AI infra", tags='["catalyst"]')
    cio("TSLA", "consumer", "ADVANCE_FORCED", rank=2, conv=62, fs=60.0, rat="floor fill")
    cio("MSFT", "technology", "REJECT", fs=55.0, rat="Valuation risk")
    conn.commit()

    with patch("loaders.db_loader.load_research_db", return_value=conn):
        yield conn
    conn.close()


def test_cio_inputs_only_recommended(mock_db):
    df = get_cio_inputs(DATE)
    assert set(df["ticker"]) == {"NVDA", "MSFT", "TSLA"}   # AMD excluded (rec=0)
    assert "OLD" not in set(df["ticker"])                  # other cycle excluded
    assert list(df.columns) == ["team_id", "ticker", "quant_rank", "quant_score", "qual_score"]


def test_cio_inputs_ordered_by_team_then_rank(mock_db):
    df = get_cio_inputs(DATE)
    # consumer before technology; within technology, rank 1 (NVDA) before 2 (MSFT)
    assert df["team_id"].tolist() == ["consumer", "technology", "technology"]
    tech = df[df["team_id"] == "technology"]
    assert tech["ticker"].tolist() == ["NVDA", "MSFT"]


def test_cio_inputs_empty_cycle(mock_db):
    assert get_cio_inputs("2099-01-01").empty


def test_cio_evaluations_all_rows(mock_db):
    df = get_cio_evaluations(DATE)
    assert set(df["ticker"]) == {"NVDA", "TSLA", "MSFT"}
    assert "rationale" in df.columns and "rule_tags" in df.columns
    assert df[df["ticker"] == "MSFT"].iloc[0]["rationale"] == "Valuation risk"


def test_cio_evaluations_ranked_first(mock_db):
    df = get_cio_evaluations(DATE)
    # ranked rows (NVDA=1, TSLA=2) sort ahead of the null-rank REJECT (MSFT)
    assert df["ticker"].tolist() == ["NVDA", "TSLA", "MSFT"]


def test_cio_evaluations_empty_cycle(mock_db):
    assert get_cio_evaluations("2099-01-01").empty
