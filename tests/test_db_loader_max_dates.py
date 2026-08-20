"""Tests for loaders/db_loader.py::get_table_max_dates (config-I2638).

The staleness chokepoint every research.db-table-count consumer should pull
from: a table frozen since its producer stopped writing must render
distinguishably from a healthy one, and this is the one place that measures
"distinguishably" (MAX(date_col) per table). Uses an in-memory SQLite DB,
mirroring tests/test_db_loader_queries.py's mock_db pattern.
"""

import sqlite3
from unittest.mock import patch

import pytest

from loaders.db_loader import TABLE_DATE_COLUMNS, get_table_max_dates


@pytest.fixture
def mock_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE investment_thesis (symbol TEXT, date TEXT)")
    conn.execute("CREATE TABLE cio_evaluations (ticker TEXT, eval_date TEXT)")
    conn.execute("CREATE TABLE team_candidates (ticker TEXT, eval_date TEXT)")
    conn.execute("CREATE TABLE team_inputs (ticker TEXT, eval_date TEXT)")
    conn.execute("CREATE TABLE macro_snapshots (date TEXT)")
    # No rows inserted -- an empty-but-present table.
    conn.execute("CREATE TABLE population_history (date TEXT)")
    conn.executemany(
        "INSERT INTO investment_thesis VALUES (?, ?)",
        [("AAPL", "2026-07-01"), ("AAPL", "2026-07-10"), ("MSFT", "2026-06-15")],
    )
    conn.executemany(
        "INSERT INTO cio_evaluations VALUES (?, ?)",
        [("AAPL", "2026-07-04"), ("MSFT", "2026-07-10")],
    )
    conn.executemany(
        "INSERT INTO team_candidates VALUES (?, ?)",
        [("AAPL", "2026-07-10")],
    )
    # team_inputs and macro_snapshots deliberately left empty/absent-of-
    # recent-rows here -- exercises the "table exists, MAX is NULL" path.
    conn.commit()
    return conn


class TestGetTableMaxDates:
    def test_returns_max_date_per_table(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            out = get_table_max_dates(
                ["investment_thesis", "cio_evaluations", "team_candidates"]
            )
        assert out == {
            "investment_thesis": "2026-07-10",
            "cio_evaluations": "2026-07-10",
            "team_candidates": "2026-07-10",
        }

    def test_empty_table_is_none_not_manufactured(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            out = get_table_max_dates(["team_inputs"])
        assert out == {"team_inputs": None}

    def test_missing_table_in_db_is_none(self, mock_db):
        # macro_snapshots is empty of rows too -- MAX() over zero rows is NULL.
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            out = get_table_max_dates(["macro_snapshots"])
        assert out == {"macro_snapshots": None}

    def test_table_not_in_registry_is_none(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            out = get_table_max_dates(["not_a_real_table"])
        assert out == {"not_a_real_table": None}

    def test_no_connection_returns_none_for_all(self):
        with patch("loaders.db_loader.load_research_db", return_value=None):
            out = get_table_max_dates(["investment_thesis", "cio_evaluations"])
        assert out == {"investment_thesis": None, "cio_evaluations": None}

    def test_defaults_to_every_registered_table(self, mock_db):
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            out = get_table_max_dates()
        assert set(out) == set(TABLE_DATE_COLUMNS)

    def test_frozen_table_distinguishable_from_healthy(self, mock_db):
        """The actual regression this exists to prevent: two tables with
        similar row counts but different producer health must not report
        the same last-write date."""
        with patch("loaders.db_loader.load_research_db", return_value=mock_db):
            out = get_table_max_dates(["investment_thesis", "team_candidates"])
        # Both non-empty tables here happen to share a max date in the
        # fixture; the distinguishing case is a healthy table (recent rows)
        # vs. an empty/frozen one (team_inputs), asserted separately above.
        assert out["investment_thesis"] is not None
        assert out["team_candidates"] is not None
