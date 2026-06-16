"""Tests for get_scanner_evaluations — the scanner-funnel loader behind the
Scanner page. Patches load_research_db to an in-memory SQLite, mirroring the
decision-review / sector-team loader test pattern.
"""

import sqlite3
from unittest.mock import patch

import pytest

from loaders.db_loader import get_scanner_evaluations

DATE = "2026-06-13"
OTHER = "2026-06-06"


@pytest.fixture
def mock_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE scanner_evaluations (ticker TEXT, eval_date TEXT, sector TEXT, "
        "tech_score REAL, scan_path TEXT, quant_filter_pass INTEGER, liquidity_pass INTEGER, "
        "volatility_pass INTEGER, balance_sheet_pass INTEGER, filter_fail_reason TEXT, "
        "rsi_14 REAL, atr_pct REAL, price_vs_ma200 REAL, current_price REAL, avg_volume_20d REAL)"
    )

    def sc(t, sector, passed, date=DATE, liq=1, reason=None, tech=50.0):
        conn.execute(
            "INSERT INTO scanner_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, date, sector, tech, "momentum", passed, liq, 1, 1, reason,
             55.0, 2.0, 1.05, 100.0, 1_000_000),
        )

    sc("NVDA", "Information Technology", 1, tech=90)
    sc("AMD", "Information Technology", 0, reason="rank_cutoff", tech=40)
    sc("PENNY", "Information Technology", 0, liq=0, reason="liquidity", tech=20)
    sc("LLY", "Health Care", 1, tech=80)
    sc("OLD", "Information Technology", 1, date=OTHER, tech=99)  # other cycle
    conn.commit()

    with patch("loaders.db_loader.load_research_db", return_value=conn):
        yield conn
    conn.close()


def test_filters_by_date(mock_db):
    df = get_scanner_evaluations(DATE)
    assert set(df["ticker"]) == {"NVDA", "AMD", "PENNY", "LLY"}
    assert "OLD" not in set(df["ticker"])  # other cycle excluded


def test_carries_gate_columns(mock_db):
    df = get_scanner_evaluations(DATE)
    for col in ("quant_filter_pass", "liquidity_pass", "volatility_pass",
                "balance_sheet_pass", "filter_fail_reason", "sector", "tech_score"):
        assert col in df.columns


def test_per_sector_aggregation_matches(mock_db):
    # The page aggregates in pandas; lock the funnel arithmetic here.
    df = get_scanner_evaluations(DATE)
    df["passed"] = df["quant_filter_pass"]
    by_sector = df.groupby("sector")["passed"].agg(["count", "sum"])
    assert by_sector.loc["Information Technology", "count"] == 3
    assert by_sector.loc["Information Technology", "sum"] == 1
    assert by_sector.loc["Health Care", "count"] == 1


def test_empty_cycle(mock_db):
    assert get_scanner_evaluations("2099-01-01").empty
