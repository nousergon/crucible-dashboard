"""Schema-drift contract guard for loaders/db_schema.py (config#963).

The dashboard reads research.db with SQL whose projections enumerate column
names as bare literals. ``loaders/db_schema.py`` centralizes those names into a
single per-table contract so a producer rename is a one-line edit; this test
gives the contract teeth by PINNING every enumerated ``db_loader`` query to it:

  * ``test_projection_columns_are_contracted`` — every named projection selects
    only columns declared in its table's contract (a static ratchet: a new
    projection that reads an undeclared column fails here);
  * ``test_projections_execute_against_contract_schema`` — each projection's
    SELECT runs cleanly against an in-memory DB built FROM the contract, so a
    projection/contract divergence surfaces as a loud test failure rather than
    the silent empty frame ``query_research_db`` would return at runtime;
  * ``test_enumerated_loaders_return_rows_against_contract_db`` — the public
    loader functions, run end-to-end against a fully-seeded contract DB, return
    non-empty frames, catching WHERE/ORDER BY/GROUP BY column drift the
    projection-only checks miss.

Together these mean: if a research.db column the dashboard reads is renamed,
the fix is to update ONE contract tuple in db_schema.py, and CI stays red until
it is — instead of a page silently blanking in production.
"""

import logging
import sqlite3
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# db_loader imports s3_loader (which uses @st.cache); mock both before import.
import sys

sys.modules.setdefault("streamlit", MagicMock())
_mock_s3 = MagicMock()
_mock_s3.load_config.return_value = {
    "s3": {"research_bucket": "test-bucket"},
    "paths": {"research_db": "research.db"},
}
sys.modules["loaders.s3_loader"] = _mock_s3

from loaders import db_loader, db_schema  # noqa: E402


# ---------------------------------------------------------------------------
# Static contract ratchet
# ---------------------------------------------------------------------------

def test_projection_columns_are_contracted():
    """Every column of every named projection must be declared in its table's
    contract — the static drift ratchet for db_loader's enumerated SELECTs."""
    problems = {}
    for name, projection in db_schema.PROJECTIONS.items():
        table = db_schema.projection_table(projection)
        assert table is not None, f"projection {name} is not registered to a table"
        undeclared = [c for c in projection if c not in db_schema.CONTRACT[table]]
        if undeclared:
            problems[name] = (table, undeclared)
    assert not problems, (
        "projections read columns absent from their table contract "
        f"(update db_schema.CONTRACT): {problems}"
    )


def test_join_rejects_uncontracted_column():
    """join() fails loud when a projection drifts a column out of its contract,
    so a typo/rename can't silently produce an empty frame at runtime."""
    bogus = ("ticker", "column_that_does_not_exist")
    db_schema._register(bogus, "cio_evaluations")
    with pytest.raises(ValueError, match="not in the 'cio_evaluations' contract"):
        db_schema.join(bogus)


def test_join_is_byte_identical_to_literal():
    """The generated SELECT list must equal the comma+space literal it replaced
    — the migration is behaviour-preserving by construction."""
    assert db_schema.join(db_schema.CIO_FUNNEL_COLS) == (
        "ticker, cio_decision, cio_rank, cio_conviction, final_score"
    )


# ---------------------------------------------------------------------------
# Runtime drift accessor
# ---------------------------------------------------------------------------

def test_warn_missing_logs_and_returns_unchanged(caplog):
    df = pd.DataFrame({"ticker": ["AAPL"], "sector": ["tech"]})
    with caplog.at_level(logging.WARNING, logger="loaders.db_schema"):
        out = db_schema.warn_missing(df, "scanner_evaluations", "ticker", "tech_score")
    assert out is df  # fail-soft: never mutates or replaces the frame
    assert "schema drift" in caplog.text
    assert "tech_score" in caplog.text


def test_warn_missing_silent_when_present_or_empty(caplog):
    full = pd.DataFrame({"ticker": ["AAPL"], "sector": ["tech"]})
    with caplog.at_level(logging.WARNING, logger="loaders.db_schema"):
        db_schema.warn_missing(full, "scanner_evaluations", "ticker", "sector")
        db_schema.warn_missing(pd.DataFrame(), "scanner_evaluations", "ticker")
    assert "schema drift" not in caplog.text


# ---------------------------------------------------------------------------
# Contract <-> SQL pin
# ---------------------------------------------------------------------------

def _contract_db() -> sqlite3.Connection:
    """In-memory research.db whose tables carry EXACTLY the contract columns."""
    conn = sqlite3.connect(":memory:")
    for table, cols in db_schema.CONTRACT.items():
        col_ddl = ", ".join(f"{c} {'INTEGER' if _is_int(c) else 'REAL' if _is_real(c) else 'TEXT'}" for c in cols)
        conn.execute(f"CREATE TABLE {table} ({col_ddl})")
    conn.commit()
    return conn


_INT_COLS = {
    "quant_filter_pass", "liquidity_pass", "volatility_pass",
    "balance_sheet_pass", "focus_list_passed", "agent_override",
    "team_recommended", "correct", "cio_rank", "quant_rank",
    "focus_rank_in_team", "focus_rank_in_sector",
}
_REAL_COLS = {
    "tech_score", "rsi_14", "atr_pct", "price_vs_ma200", "current_price",
    "avg_volume_20d", "focus_score", "quant_score", "qual_score",
    "combined_score", "macro_shift", "final_score", "cio_conviction",
    "rsi_sub_score", "macd_sub_score", "ma50_sub_score", "ma200_sub_score",
    "momentum_sub_score", "p_up", "actual_log_alpha",
}


def _is_int(col: str) -> bool:
    return col in _INT_COLS


def _is_real(col: str) -> bool:
    return col in _REAL_COLS


def test_projections_execute_against_contract_schema():
    """Each named projection's SELECT must execute against a contract-built
    schema — proving every projected column is declared."""
    conn = _contract_db()
    failures = {}
    for name, projection in db_schema.PROJECTIONS.items():
        table = db_schema.projection_table(projection)
        try:
            conn.execute(f"SELECT {db_schema.join(projection)} FROM {table} LIMIT 1")
        except sqlite3.OperationalError as e:  # pragma: no cover - failure path
            failures[name] = str(e)
    assert not failures, f"projections reference undeclared columns: {failures}"


# ---------------------------------------------------------------------------
# End-to-end: loaders return rows against a fully-seeded contract DB
# ---------------------------------------------------------------------------

_EVAL_DATE = "2026-06-01"
_TEAM = "technology"
_TICKER = "AAPL"


def _seed_row(conn: sqlite3.Connection, table: str, **overrides) -> None:
    cols = db_schema.CONTRACT[table]
    values = []
    for c in cols:
        if c in overrides:
            values.append(overrides[c])
        elif _is_int(c):
            values.append(1)
        elif _is_real(c):
            values.append(0.5)
        else:
            values.append("x")
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", values
    )
    conn.commit()


@pytest.fixture
def seeded_contract_db():
    conn = _contract_db()
    _seed_row(conn, "scanner_evaluations", ticker=_TICKER, eval_date=_EVAL_DATE,
              sector="tech", focus_list_passed=1, agent_override=1,
              focus_team_id=_TEAM, quant_filter_pass=1)
    _seed_row(conn, "team_candidates", ticker=_TICKER, eval_date=_EVAL_DATE,
              team_id=_TEAM, team_recommended=1)
    _seed_row(conn, "cio_evaluations", ticker=_TICKER, eval_date=_EVAL_DATE,
              team_id=_TEAM, cio_decision="ADVANCE")
    _seed_row(conn, "team_inputs", ticker=_TICKER, eval_date=_EVAL_DATE,
              team_id=_TEAM, source="scanner", sector="tech")
    _seed_row(conn, "predictor_outcomes", model_version="v1",
              prediction_date=_EVAL_DATE, p_up=0.6, actual_log_alpha=0.02, correct=1)
    return conn


def test_enumerated_loaders_return_rows_against_contract_db(seeded_contract_db):
    """The public loaders (including their WHERE/ORDER BY/GROUP BY column
    references, not just the SELECT projection) run against a contract-shaped DB
    and return rows — the end-to-end pin that a column the dashboard depends on
    is present in the contract."""
    conn = seeded_contract_db
    with patch.object(db_loader, "load_research_db", return_value=conn):
        checks = {
            "get_cio_inputs": db_loader.get_cio_inputs(_EVAL_DATE),
            "get_cio_evaluations": db_loader.get_cio_evaluations(_EVAL_DATE),
            "get_team_candidates": db_loader.get_team_candidates(_EVAL_DATE, _TEAM),
            "get_team_inputs": db_loader.get_team_inputs(_EVAL_DATE, _TEAM),
            "get_scanner_evaluations": db_loader.get_scanner_evaluations(_EVAL_DATE),
            "get_focus_list_audit": db_loader.get_focus_list_audit(),
            "get_model_version_scorecard": db_loader.get_model_version_scorecard(),
        }
    empty = [name for name, df in checks.items() if df is None or df.empty]
    assert not empty, (
        f"loaders returned empty against a contract-shaped, seeded DB — a query "
        f"references a column not in the db_schema contract: {empty}"
    )


def test_cycle_funnel_reads_against_contract_db(seeded_contract_db):
    """get_cycle_funnel unions scanner/team/cio reads — its cio projection is
    contracted; assert it resolves the advanced set against the contract DB."""
    conn = seeded_contract_db
    with patch.object(db_loader, "load_research_db", return_value=conn):
        funnel = db_loader.get_cycle_funnel(_EVAL_DATE)
    assert funnel["cio_evaluated"] == 1
    assert funnel["cio_advanced"] == 1  # cio_decision="ADVANCE" resolved
