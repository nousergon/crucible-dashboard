"""Tests for the decision-review loader functions (L4567 Phase 3).

Patches load_research_db to an in-memory SQLite seeded with the decision
audit tables, mirroring the existing db_loader test pattern.
"""

import sqlite3
from unittest.mock import patch

import pytest

from loaders.db_loader import (
    explain_why_not,
    get_cycle_funnel,
    get_decision_eval_dates,
    get_ticker_decision,
)

DATE = "2026-05-16"


@pytest.fixture
def mock_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE scanner_evaluations (ticker TEXT, eval_date TEXT, "
        "quant_filter_pass INTEGER, filter_fail_reason TEXT, liquidity_pass INTEGER, "
        "volatility_pass INTEGER, balance_sheet_pass INTEGER, tech_score REAL)"
    )
    conn.execute(
        "CREATE TABLE team_candidates (ticker TEXT, eval_date TEXT, team_id TEXT, "
        "quant_rank INTEGER, quant_score REAL, qual_score REAL, team_recommended INTEGER)"
    )
    conn.execute(
        "CREATE TABLE cio_evaluations (ticker TEXT, eval_date TEXT, cio_decision TEXT, "
        "cio_rank INTEGER, cio_conviction INTEGER, final_score REAL, rationale TEXT)"
    )
    conn.execute(
        "CREATE TABLE investment_thesis (symbol TEXT, date TEXT, run_time TEXT, "
        "rating TEXT, score REAL, thesis_summary TEXT)"
    )

    def sc(t, p, reason=None, liq=1, tech=50.0):
        conn.execute("INSERT INTO scanner_evaluations VALUES (?,?,?,?,?,?,?,?)",
                     (t, DATE, p, reason, liq, 1, 1, tech))

    def tc(t, team, rank, q, ql, rec):
        conn.execute("INSERT INTO team_candidates VALUES (?,?,?,?,?,?,?)",
                     (t, DATE, team, rank, q, ql, rec))

    def cio(t, dec, rank=None, conv=None, fs=None, rat=""):
        conn.execute("INSERT INTO cio_evaluations VALUES (?,?,?,?,?,?,?)",
                     (t, DATE, dec, rank, conv, fs, rat))

    sc("NVDA", 1, tech=88.0); tc("NVDA", "technology", 1, 85, 70, 1)
    cio("NVDA", "ADVANCE", 1, 80, 82.0, "AI infra")
    sc("MSFT", 1); tc("MSFT", "technology", 11, 47, 40, 0)
    sc("PENNY", 0, reason="liquidity", liq=0, tech=20.0)
    sc("TSLA", 1); tc("TSLA", "consumer", 2, 66, 60, 1)
    cio("TSLA", "REJECT", fs=55.0, rat="Valuation risk")
    sc("WING", 1); tc("WING", "consumer", 3, 58, 55, 1)
    cio("WING", "ADVANCE_FORCED", 3, 62, 60.0, "floor fill")
    sc("ORCL", 1)  # passed scanner, no team row
    conn.execute("INSERT INTO investment_thesis VALUES (?,?,?,?,?,?)",
                 ("NVDA", DATE, "2026-05-16T09:00Z", "BUY", 82.0, "datacenter demand"))
    conn.commit()

    with patch("loaders.db_loader.load_research_db", return_value=conn):
        yield conn
    conn.close()


def test_eval_dates(mock_db):
    assert get_decision_eval_dates() == [DATE]


def test_get_ticker_decision(mock_db):
    rec = get_ticker_decision("nvda", DATE)  # lowercase normalized
    assert not rec["scanner"].empty
    assert rec["team_candidates"].iloc[0]["team_recommended"] == 1
    assert rec["cio"].iloc[0]["cio_decision"] == "ADVANCE"
    assert rec["thesis"].iloc[0]["rating"] == "BUY"


def test_why_not_scanner(mock_db):
    r = explain_why_not("PENNY", DATE)
    assert r["stage"] == "scanner"
    assert "liquidity" in r["verdict"]


def test_why_not_team_not_recommended(mock_db):
    r = explain_why_not("MSFT", DATE)
    assert r["stage"] == "team"
    assert "NOT recommended" in r["verdict"]
    assert "NVDA" in r["verdict"]  # comparison context: the recommended pick


def test_why_not_passed_scanner_no_team(mock_db):
    r = explain_why_not("ORCL", DATE)
    assert r["stage"] == "team"
    assert "did not appear" in r["verdict"]


def test_why_not_cio_reject(mock_db):
    r = explain_why_not("TSLA", DATE)
    assert r["stage"] == "cio"
    assert "Valuation risk" in r["verdict"]


def test_why_not_chosen(mock_db):
    r = explain_why_not("NVDA", DATE)
    assert r["stage"] == "chosen"
    assert "WAS chosen" in r["verdict"]


def test_why_not_chosen_advance_forced(mock_db):
    r = explain_why_not("WING", DATE)
    assert r["stage"] == "chosen"  # ADVANCE_FORCED counts as chosen


def test_why_not_no_record(mock_db):
    r = explain_why_not("GHOST", DATE)
    assert r["stage"] == "no_record"


def test_cycle_funnel(mock_db):
    f = get_cycle_funnel(DATE)
    assert f["scanner_screened"] == 6  # NVDA MSFT PENNY TSLA WING ORCL
    assert f["scanner_passed"] == 5    # all but PENNY
    assert f["team_ranked"] == 4       # NVDA MSFT TSLA WING
    assert f["team_recommended"] == 3  # NVDA TSLA WING
    assert f["cio_evaluated"] == 3     # NVDA TSLA WING
    assert f["cio_advanced"] == 2      # NVDA (ADVANCE) + WING (ADVANCE_FORCED)
