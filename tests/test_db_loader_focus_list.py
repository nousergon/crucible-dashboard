"""Tests for db_loader focus-list audit functions (PR 7 of scanner-placement
arc, ``alpha-engine-docs/private/scanner-260514.md``).

Uses an in-memory SQLite that mirrors the scanner_evaluations v17 schema
(per alpha-engine-research#183). Covers empty/missing-data graceful
degrade + aggregate math + filter behavior.
"""

import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from loaders.db_loader import (
    get_focus_list_audit,
    get_focus_list_stance_mix,
    get_focus_list_weekly_summary,
)


@pytest.fixture
def mock_scanner_evals_db():
    """In-memory SQLite with v17 scanner_evaluations schema + seed data."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE scanner_evaluations (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            eval_date TEXT NOT NULL,
            sector TEXT,
            tech_score REAL,
            scan_path TEXT,
            quant_filter_pass INTEGER NOT NULL DEFAULT 0,
            liquidity_pass INTEGER NOT NULL DEFAULT 1,
            volatility_pass INTEGER NOT NULL DEFAULT 1,
            balance_sheet_pass INTEGER NOT NULL DEFAULT 1,
            filter_fail_reason TEXT,
            rsi_14 REAL, atr_pct REAL, price_vs_ma200 REAL,
            current_price REAL, avg_volume_20d REAL,
            focus_score REAL,
            focus_stance TEXT,
            focus_team_id TEXT,
            focus_rank_in_team INTEGER,
            focus_rank_in_sector INTEGER,
            focus_list_passed INTEGER NOT NULL DEFAULT 0,
            agent_override INTEGER NOT NULL DEFAULT 0,
            UNIQUE(ticker, eval_date)
        )
    """)
    # Seed: 2 weekly runs, 1 team (technology) with 3 focus-list members + 1 override
    # Week 1 (2026-05-17): NVDA + MSFT in focus list, NVDA picked. CRM override, picked.
    # Week 2 (2026-05-24): NVDA + AAPL in focus list, both picked. No overrides.
    rows = [
        # ticker, eval_date, sector, focus_score, focus_stance, focus_team_id,
        # focus_rank_in_team, focus_rank_in_sector, focus_list_passed,
        # agent_override, quant_filter_pass
        ("NVDA", "2026-05-17", "Technology", 85.0, "momentum", "technology",
         1, 1, 1, 0, 1),
        ("MSFT", "2026-05-17", "Technology", 72.0, "quality", "technology",
         2, 2, 1, 0, 0),  # in focus, not picked
        ("CRM",  "2026-05-17", "Technology", None, None, None,
         None, None, 0, 1, 1),  # override, picked
        ("NVDA", "2026-05-24", "Technology", 88.0, "momentum", "technology",
         1, 1, 1, 0, 1),
        ("AAPL", "2026-05-24", "Technology", 70.0, "quality", "technology",
         2, 2, 1, 0, 1),
    ]
    conn.executemany(
        """INSERT INTO scanner_evaluations
           (ticker, eval_date, sector, focus_score, focus_stance, focus_team_id,
            focus_rank_in_team, focus_rank_in_sector, focus_list_passed,
            agent_override, quant_filter_pass)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return conn


@pytest.fixture
def empty_db():
    """In-memory SQLite with the schema but no data (Saturday 5/17 pre-fire)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE scanner_evaluations (
            id INTEGER PRIMARY KEY,
            ticker TEXT, eval_date TEXT, sector TEXT,
            tech_score REAL, scan_path TEXT,
            quant_filter_pass INTEGER DEFAULT 0,
            liquidity_pass INTEGER DEFAULT 1,
            volatility_pass INTEGER DEFAULT 1,
            balance_sheet_pass INTEGER DEFAULT 1,
            filter_fail_reason TEXT,
            rsi_14 REAL, atr_pct REAL, price_vs_ma200 REAL,
            current_price REAL, avg_volume_20d REAL,
            focus_score REAL, focus_stance TEXT, focus_team_id TEXT,
            focus_rank_in_team INTEGER, focus_rank_in_sector INTEGER,
            focus_list_passed INTEGER DEFAULT 0,
            agent_override INTEGER DEFAULT 0
        )
    """)
    return conn


# ── get_focus_list_audit ────────────────────────────────────────────────────


class TestGetFocusListAudit:
    def test_returns_all_rows(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_audit()
        assert len(df) == 5
        assert set(df["ticker"]) == {"NVDA", "MSFT", "CRM", "AAPL"}

    def test_filters_by_date_range(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_audit(start_date="2026-05-24")
        assert len(df) == 2
        assert set(df["ticker"]) == {"NVDA", "AAPL"}

    def test_end_date_filter(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_audit(end_date="2026-05-17")
        assert len(df) == 3

    def test_empty_db_returns_empty_dataframe(self, empty_db):
        with patch("loaders.db_loader.load_research_db", return_value=empty_db):
            df = get_focus_list_audit()
        assert df.empty

    def test_audit_columns_present(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_audit()
        expected = {
            "ticker", "eval_date", "sector",
            "focus_score", "focus_stance", "focus_team_id",
            "focus_rank_in_team", "focus_rank_in_sector",
            "focus_list_passed", "agent_override", "quant_filter_pass",
        }
        assert expected.issubset(set(df.columns))


# ── get_focus_list_weekly_summary ───────────────────────────────────────────


class TestGetFocusListWeeklySummary:
    def test_aggregates_per_team_per_week(self, mock_scanner_evals_db):
        """Override-only rows (focus_team_id=NULL) form their own group —
        archive_writer doesn't carry per-team attribution for tool-overrides
        outside the focus list. Week 1 has 2 groups (technology + NULL);
        Week 2 has 1 group (technology only, no overrides). Total = 3 rows."""
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_weekly_summary()
        assert len(df) == 3
        w1_tech = df[
            (df["eval_date"] == "2026-05-17") & (df["focus_team_id"] == "technology")
        ].iloc[0]
        assert w1_tech["n_focus_list"] == 2
        assert w1_tech["n_picks"] == 1  # only NVDA (CRM is in its own NULL group)
        assert w1_tech["n_overrides"] == 0
        assert w1_tech["n_focus_and_picked"] == 1  # NVDA

        w1_override = df[
            (df["eval_date"] == "2026-05-17") & (df["focus_team_id"].isna())
        ].iloc[0]
        assert w1_override["n_focus_list"] == 0
        assert w1_override["n_picks"] == 1  # CRM
        assert w1_override["n_overrides"] == 1
        assert w1_override["n_override_and_picked"] == 1

    def test_precision_recall_override_rates(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_weekly_summary()
        # Team-attributed precision/recall (technology, Week 1): 1/2 each
        w1_tech = df[
            (df["eval_date"] == "2026-05-17") & (df["focus_team_id"] == "technology")
        ].iloc[0]
        assert w1_tech["precision"] == pytest.approx(0.5)  # NVDA/(NVDA+MSFT)
        assert w1_tech["recall"] == pytest.approx(1.0)  # NVDA/(only NVDA picked in this group)
        assert pd.isna(w1_tech["override_hit_rate"])  # no overrides for technology row

        # Override-only Week 1 group — override hit rate should be 1.0 (CRM hit)
        w1_override = df[
            (df["eval_date"] == "2026-05-17") & (df["focus_team_id"].isna())
        ].iloc[0]
        assert w1_override["override_hit_rate"] == pytest.approx(1.0)

        # Week 2 (clean — both focus-list members picked)
        w2 = df[df["eval_date"] == "2026-05-24"].iloc[0]
        assert w2["precision"] == pytest.approx(1.0)
        assert w2["recall"] == pytest.approx(1.0)
        assert pd.isna(w2["override_hit_rate"])

    def test_empty_db_returns_empty_dataframe(self, empty_db):
        with patch("loaders.db_loader.load_research_db", return_value=empty_db):
            df = get_focus_list_weekly_summary()
        assert df.empty


# ── get_focus_list_stance_mix ───────────────────────────────────────────────


class TestGetFocusListStanceMix:
    def test_default_returns_latest_week(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_stance_mix()
        # Latest week (2026-05-24): 1 momentum (NVDA) + 1 quality (AAPL)
        assert len(df) == 2
        assert set(df["focus_stance"]) == {"momentum", "quality"}
        for _, row in df.iterrows():
            assert row["n"] == 1

    def test_specific_date_returns_that_week(self, mock_scanner_evals_db):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_stance_mix(eval_date="2026-05-17")
        # Week 1: 1 momentum (NVDA) + 1 quality (MSFT). CRM is override
        # (focus_list_passed=0), so doesn't surface in stance mix.
        assert len(df) == 2
        assert set(df["focus_stance"]) == {"momentum", "quality"}

    def test_empty_db_returns_empty(self, empty_db):
        with patch("loaders.db_loader.load_research_db", return_value=empty_db):
            df = get_focus_list_stance_mix()
        assert df.empty


# ── config#750: per-team override attribution (v23) ─────────────────────────


@pytest.fixture
def mock_scanner_evals_db_v23():
    """In-memory SQLite with the v23 scanner_evaluations schema (adds
    override_team_id) + seed data where two teams each override one ticker."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE scanner_evaluations (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            eval_date TEXT NOT NULL,
            sector TEXT,
            tech_score REAL, scan_path TEXT,
            quant_filter_pass INTEGER NOT NULL DEFAULT 0,
            liquidity_pass INTEGER NOT NULL DEFAULT 1,
            volatility_pass INTEGER NOT NULL DEFAULT 1,
            balance_sheet_pass INTEGER NOT NULL DEFAULT 1,
            filter_fail_reason TEXT,
            rsi_14 REAL, atr_pct REAL, price_vs_ma200 REAL,
            current_price REAL, avg_volume_20d REAL,
            focus_score REAL, focus_stance TEXT, focus_team_id TEXT,
            focus_rank_in_team INTEGER, focus_rank_in_sector INTEGER,
            focus_list_passed INTEGER NOT NULL DEFAULT 0,
            agent_override INTEGER NOT NULL DEFAULT 0,
            override_team_id TEXT,
            UNIQUE(ticker, eval_date)
        )
    """)
    rows = [
        # ticker, eval_date, sector, focus_score, focus_stance, focus_team_id,
        # focus_rank_in_team, focus_rank_in_sector, focus_list_passed,
        # agent_override, quant_filter_pass, override_team_id
        # Week 1: technology focus list (NVDA picked, MSFT not) + one override
        # per team: TSLA overridden by technology (hit), XOM by energy (miss).
        ("NVDA", "2026-05-17", "Technology", 85.0, "momentum", "technology",
         1, 1, 1, 0, 1, None),
        ("MSFT", "2026-05-17", "Technology", 72.0, "quality", "technology",
         2, 2, 1, 0, 0, None),
        ("TSLA", "2026-05-17", "Technology", None, None, None,
         None, None, 0, 1, 1, "technology"),   # tech override, picked
        ("XOM",  "2026-05-17", "Energy", None, None, None,
         None, None, 0, 1, 0, "energy"),        # energy override, missed
    ]
    conn.executemany(
        """INSERT INTO scanner_evaluations
           (ticker, eval_date, sector, focus_score, focus_stance, focus_team_id,
            focus_rank_in_team, focus_rank_in_sector, focus_list_passed,
            agent_override, quant_filter_pass, override_team_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return conn


class TestPerTeamOverrideAttributionV23:
    def test_overrides_attributed_to_overriding_team(
        self, mock_scanner_evals_db_v23
    ):
        """config#750: TSLA's override counts against technology and XOM's
        against energy — no anonymous NULL group when override_team_id is set."""
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db_v23,
        ):
            df = get_focus_list_weekly_summary()
        # No NULL/anonymous team row — every override is attributed.
        assert df["focus_team_id"].isna().sum() == 0
        tech = df[df["focus_team_id"] == "technology"].iloc[0]
        energy = df[df["focus_team_id"] == "energy"].iloc[0]
        # technology row carries its focus list AND its one override.
        assert tech["n_focus_list"] == 2
        assert tech["n_overrides"] == 1
        assert tech["override_hit_rate"] == pytest.approx(1.0)  # TSLA hit
        # energy row is override-only, and its override missed.
        assert energy["n_focus_list"] == 0
        assert energy["n_overrides"] == 1
        assert energy["override_hit_rate"] == pytest.approx(0.0)  # XOM missed

    def test_audit_exposes_override_team_id(self, mock_scanner_evals_db_v23):
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db_v23,
        ):
            df = get_focus_list_audit()
        assert "override_team_id" in df.columns
        by_ticker = df.set_index("ticker")["override_team_id"].to_dict()
        assert by_ticker["TSLA"] == "technology"
        assert by_ticker["XOM"] == "energy"
        assert by_ticker["NVDA"] is None or pd.isna(by_ticker["NVDA"])

    def test_pre_v23_db_gracefully_projects_null_override_team(
        self, mock_scanner_evals_db
    ):
        """On a pre-v23 DB (no override_team_id column) the audit query still
        succeeds and projects a NULL override_team_id column."""
        with patch(
            "loaders.db_loader.load_research_db",
            return_value=mock_scanner_evals_db,
        ):
            df = get_focus_list_audit()
        assert "override_team_id" in df.columns
        assert df["override_team_id"].isna().all()
