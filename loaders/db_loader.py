"""
SQLite research.db loader for the Alpha Engine Dashboard.
Downloads research.db from S3 to /tmp and queries it via sqlite3.
"""

import logging
import sqlite3
import os

import pandas as pd
import streamlit as st

from loaders.s3_loader import load_config, download_s3_binary

logger = logging.getLogger(__name__)

_DB_LOCAL_PATH = "/tmp/research.db"
_DB_BUCKET_KEY = "research.db"


def _get_research_bucket() -> str:
    return load_config()["s3"]["research_bucket"]


def _get_db_path_key() -> str:
    return load_config()["paths"]["research_db"]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False, ttl=3600)
def load_research_db() -> sqlite3.Connection | None:
    """
    Download research.db from S3 to /tmp/research.db and return a sqlite3
    connection. Returns None on failure. Cached for 1 hour — re-downloads
    from S3 on expiry to pick up new data and recover from stale connections.
    """
    try:
        bucket = _get_research_bucket()
        key = _get_db_path_key()
        success = download_s3_binary(bucket, key, _DB_LOCAL_PATH)
        if not success:
            return None
        conn = sqlite3.connect(_DB_LOCAL_PATH, check_same_thread=False)
        return conn
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Query helper
# ---------------------------------------------------------------------------


def query_research_db(sql: str, params=None) -> pd.DataFrame:
    """
    Execute *sql* against research.db and return a DataFrame.
    Returns an empty DataFrame on any failure.
    """
    conn = load_research_db()
    if conn is None:
        return pd.DataFrame()
    try:
        if params:
            return pd.read_sql_query(sql, conn, params=params)
        return pd.read_sql_query(sql, conn)
    except Exception as e:
        logger.warning("Query failed: %s — %s", sql[:100], e)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Named queries
# ---------------------------------------------------------------------------


def _normalize_score_col(df: pd.DataFrame) -> pd.DataFrame:
    """Alias 'score' → 'composite_score' for backward compat with dashboard pages."""
    if not df.empty and "score" in df.columns and "composite_score" not in df.columns:
        df = df.rename(columns={"score": "composite_score"})
    return df


_MAX_QUERY_ROWS = 50_000  # safety cap to prevent OOM on t3.micro


def get_score_performance() -> pd.DataFrame:
    """
    Return rows from score_performance ordered by score_date ascending.
    Capped at _MAX_QUERY_ROWS most recent rows for memory safety.
    """
    sql = f"SELECT * FROM score_performance ORDER BY score_date DESC LIMIT {_MAX_QUERY_ROWS}"
    df = _normalize_score_col(query_research_db(sql))
    if not df.empty:
        df = df.sort_values("score_date", ascending=True).reset_index(drop=True)
    return df


def get_investment_thesis(symbol: str | None = None) -> pd.DataFrame:
    """
    Return rows from investment_thesis, optionally filtered by symbol.
    """
    if symbol:
        sql = "SELECT * FROM investment_thesis WHERE symbol = ? ORDER BY date DESC LIMIT 1000"
        return query_research_db(sql, params=(symbol,))
    return query_research_db(
        f"SELECT * FROM investment_thesis ORDER BY date DESC LIMIT {_MAX_QUERY_ROWS}"
    )


def get_macro_snapshots() -> pd.DataFrame:
    """
    Return all rows from macro_snapshots ordered by date ascending.
    Expected columns: date, regime, vix, yield_10yr, ...
    """
    sql = "SELECT * FROM macro_snapshots ORDER BY date"
    return query_research_db(sql)


def get_distinct_symbols() -> list[str]:
    """
    Return sorted list of distinct symbols from investment_thesis.
    """
    df = query_research_db(
        "SELECT DISTINCT symbol FROM investment_thesis ORDER BY symbol"
    )
    if df.empty or "symbol" not in df.columns:
        return []
    return df["symbol"].dropna().tolist()


def get_score_history(symbol: str) -> pd.DataFrame:
    """
    Return score history rows for a single symbol from score_performance.
    """
    sql = """
        SELECT score_date, score, beat_spy_10d, beat_spy_30d,
               return_10d, return_30d, spy_10d_return, spy_30d_return
        FROM score_performance
        WHERE symbol = ?
        ORDER BY score_date
    """
    return _normalize_score_col(query_research_db(sql, params=(symbol,)))


def get_top_recent_symbols(n: int = 10) -> pd.DataFrame:
    """
    Return the top *n* symbols by most recent score_date and highest composite_score.
    """
    sql = """
        SELECT sp.*
        FROM score_performance sp
        INNER JOIN (
            SELECT symbol, MAX(score_date) AS max_date
            FROM score_performance
            GROUP BY symbol
        ) latest ON sp.symbol = latest.symbol AND sp.score_date = latest.max_date
        ORDER BY sp.score DESC
        LIMIT ?
    """
    return _normalize_score_col(query_research_db(sql, params=(n,)))


def get_predictor_outcomes(symbol: str | None = None) -> pd.DataFrame:
    """Query predictor_outcomes table. Returns empty DataFrame if table missing."""
    if symbol:
        return query_research_db(
            "SELECT * FROM predictor_outcomes WHERE symbol = ? ORDER BY prediction_date DESC LIMIT 1000",
            params=(symbol,),
        )
    return query_research_db(
        f"SELECT * FROM predictor_outcomes ORDER BY prediction_date DESC LIMIT {_MAX_QUERY_ROWS}"
    )


def _per_version_metrics(df: pd.DataFrame, stage: str) -> pd.DataFrame:
    """Per-model-version realized scorecard from a resolved-outcomes frame.

    Computes, per ``model_version``: cross-sectional rank IC (Fama-MacBeth —
    per-date Spearman(p_up, actual_log_alpha), then averaged across dates, the
    way the system actually trades), hit-rate (mean ``correct``), and counts.
    Empty frame in → empty frame out.
    """
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["p_up"] = pd.to_numeric(d["p_up"], errors="coerce")
    d["actual_log_alpha"] = pd.to_numeric(d["actual_log_alpha"], errors="coerce")
    d["correct"] = pd.to_numeric(d["correct"], errors="coerce")
    d["model_version"] = d["model_version"].fillna("champion-legacy")

    rows = []
    for version, g in d.groupby("model_version"):
        # Per-date Spearman, then mean (cross-sectional, not pooled). A date
        # with <2 finite name-pairs or zero variance yields NaN and is dropped.
        per_date = []
        for _, day in g.groupby("prediction_date"):
            sub = day[["p_up", "actual_log_alpha"]].dropna()
            if len(sub) >= 2 and sub["p_up"].std() > 0 and sub["actual_log_alpha"].std() > 0:
                # Spearman = Pearson on ranks — computed scipy-free (the
                # dashboard image omits scipy; pandas method="spearman" imports it).
                per_date.append(sub["p_up"].rank().corr(sub["actual_log_alpha"].rank()))
        rank_ic = float(pd.Series(per_date).mean()) if per_date else float("nan")
        rows.append({
            "model_version": version,
            "stage": stage,
            "rank_ic": rank_ic,
            "hit_rate": float(g["correct"].mean()) if g["correct"].notna().any() else float("nan"),
            "n_predictions": int(len(g)),
            "n_dates": int(g["prediction_date"].nunique()),
        })
    return pd.DataFrame(rows)


def get_model_version_scorecard() -> pd.DataFrame:
    """Champion/challenger per-version realized scorecard (L4469 Phase 3).

    Unions the live champion outcomes (``predictor_outcomes``) with the
    challenger outcomes (``predictor_outcomes_shadow``, written by the Phase-1
    shadow runner + scored in Phase 2), computing per-version rank-IC + hit-rate
    so the operator can see which model version actually has out-of-sample edge
    before promoting one to champion. Returns columns: model_version, stage,
    rank_ic, hit_rate, n_predictions, n_dates — sorted by rank_ic desc. Empty
    until challengers exist; the champion's own scorecard shows immediately.
    Missing shadow table degrades to champion-only (query returns empty).
    """
    cols = "model_version, prediction_date, p_up, actual_log_alpha, correct"
    live = query_research_db(
        f"SELECT {cols} FROM predictor_outcomes WHERE actual_log_alpha IS NOT NULL"
    )
    shadow = query_research_db(
        f"SELECT {cols} FROM predictor_outcomes_shadow WHERE actual_log_alpha IS NOT NULL"
    )
    parts = [
        _per_version_metrics(live, "champion"),
        _per_version_metrics(shadow, "challenger"),
    ]
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values("rank_ic", ascending=False, na_position="last").reset_index(drop=True)


def canonicalize_predictor_outcomes(df: pd.DataFrame) -> pd.DataFrame:
    """Add `_resolved` (0/1, nullable) and `_realized_alpha` columns to a
    `predictor_outcomes` frame by coalescing canonical 21d columns onto
    legacy 5d columns.

    `alpha-engine-data/collectors/signal_returns.py` stopped dual-writing
    legacy columns at the 2026-05-09 canonical-alpha cutover. Every row
    written after that date has `correct` / `actual_log_alpha` populated
    and `correct_5d` / `actual_5d_return` NULL. Pre-cutover rows are the
    reverse. Reading only the legacy columns silently drops every live
    prediction; this helper makes the COALESCE explicit at the call site.

    Returns the input frame with two new columns. Idempotent on already-
    canonicalized frames. No-op on an empty frame.
    """
    if df.empty:
        return df
    out = df.copy()
    canonical = pd.to_numeric(out["correct"], errors="coerce") if "correct" in out.columns else None
    legacy = pd.to_numeric(out["correct_5d"], errors="coerce") if "correct_5d" in out.columns else None
    if canonical is not None and legacy is not None:
        out["_resolved"] = canonical.combine_first(legacy)
    elif canonical is not None:
        out["_resolved"] = canonical
    elif legacy is not None:
        out["_resolved"] = legacy
    else:
        out["_resolved"] = pd.Series([None] * len(out), index=out.index)

    canonical_alpha = pd.to_numeric(out["actual_log_alpha"], errors="coerce") if "actual_log_alpha" in out.columns else None
    legacy_return = pd.to_numeric(out["actual_5d_return"], errors="coerce") if "actual_5d_return" in out.columns else None
    if canonical_alpha is not None and legacy_return is not None:
        out["_realized_alpha"] = canonical_alpha.combine_first(legacy_return)
    elif canonical_alpha is not None:
        out["_realized_alpha"] = canonical_alpha
    elif legacy_return is not None:
        out["_realized_alpha"] = legacy_return
    else:
        out["_realized_alpha"] = pd.Series([None] * len(out), index=out.index)
    return out


# ---------------------------------------------------------------------------
# Focus list audit (scanner-placement arc, PR 7)
# ---------------------------------------------------------------------------
#
# scanner_evaluations gained focus_* + agent_override columns in v17 schema
# migration (alpha-engine-research #183). First audit data appears on the
# Saturday SF run after that migration ran. All loaders below gracefully
# return empty DataFrames when the columns are absent or no rows match.


def get_focus_list_audit(
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Per-ticker focus_list audit rows from scanner_evaluations.

    Returns the canonical audit columns: ticker, eval_date, sector,
    focus_score, focus_stance, focus_team_id, focus_rank_in_team,
    focus_rank_in_sector, focus_list_passed, agent_override,
    quant_filter_pass. Optional date range filter on eval_date.

    Empty DataFrame on any of: missing columns (pre-v17 DB), no rows,
    SQL failure.
    """
    where = []
    params: list = []
    if start_date:
        where.append("eval_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("eval_date <= ?")
        params.append(end_date)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT
            ticker, eval_date, sector,
            focus_score, focus_stance, focus_team_id,
            focus_rank_in_team, focus_rank_in_sector,
            focus_list_passed, agent_override,
            quant_filter_pass
        FROM scanner_evaluations
        {where_sql}
        ORDER BY eval_date DESC, focus_team_id, focus_rank_in_team
        LIMIT {_MAX_QUERY_ROWS}
    """
    return query_research_db(sql, params=tuple(params) if params else None)


def get_focus_list_weekly_summary() -> pd.DataFrame:
    """Per-week per-team funnel cardinality + override-rate summary.

    Aggregates scanner_evaluations to one row per (eval_date, focus_team_id):
      - n_focus_list       — count of focus_list_passed=1
      - n_picks            — count of quant_filter_pass=1 (agent picked)
      - n_overrides        — count of agent_override=1
      - precision          — focus_list_passed=1 AND quant_filter_pass=1
                             / focus_list_passed=1  (was the focus list
                             a good predictor of agent picks?)
      - recall             — focus_list_passed=1 AND quant_filter_pass=1
                             / quant_filter_pass=1  (did the focus list
                             cover the agent's picks?)
      - override_hit_rate  — agent_override=1 AND quant_filter_pass=1
                             / agent_override=1  (when the agent reached
                             outside the focus list, was it right?)

    Empty DataFrame when the focus_list columns are absent or no rows have
    focus_list_passed flags set yet (pre-Sat-5/17 SF).
    """
    sql = f"""
        SELECT
            eval_date,
            focus_team_id,
            SUM(CASE WHEN focus_list_passed = 1 THEN 1 ELSE 0 END) AS n_focus_list,
            SUM(CASE WHEN quant_filter_pass = 1 THEN 1 ELSE 0 END) AS n_picks,
            SUM(CASE WHEN agent_override = 1 THEN 1 ELSE 0 END) AS n_overrides,
            SUM(CASE WHEN focus_list_passed = 1 AND quant_filter_pass = 1 THEN 1 ELSE 0 END) AS n_focus_and_picked,
            SUM(CASE WHEN agent_override = 1 AND quant_filter_pass = 1 THEN 1 ELSE 0 END) AS n_override_and_picked
        FROM scanner_evaluations
        WHERE focus_team_id IS NOT NULL OR agent_override = 1
        GROUP BY eval_date, focus_team_id
        ORDER BY eval_date DESC, focus_team_id
        LIMIT {_MAX_QUERY_ROWS}
    """
    df = query_research_db(sql)
    if df.empty:
        return df
    # Derived rates — guard against zero denominators
    df["precision"] = df.apply(
        lambda r: (r["n_focus_and_picked"] / r["n_focus_list"])
        if r["n_focus_list"] else None,
        axis=1,
    )
    df["recall"] = df.apply(
        lambda r: (r["n_focus_and_picked"] / r["n_picks"])
        if r["n_picks"] else None,
        axis=1,
    )
    df["override_hit_rate"] = df.apply(
        lambda r: (r["n_override_and_picked"] / r["n_overrides"])
        if r["n_overrides"] else None,
        axis=1,
    )
    return df


def get_focus_list_stance_mix(eval_date: str | None = None) -> pd.DataFrame:
    """Per-team stance distribution for the focus list.

    Returns rows of (focus_team_id, focus_stance, n) for the most recent
    eval_date (or specified date). Surfaces regime/stance mismatches —
    e.g. a BULL-regime run that surfaces mostly low_vol stances flags
    blend-weight miscalibration.
    """
    if eval_date is None:
        date_sql = (
            "(SELECT MAX(eval_date) FROM scanner_evaluations "
            "WHERE focus_list_passed = 1)"
        )
        sql = f"""
            SELECT focus_team_id, focus_stance, COUNT(*) AS n
            FROM scanner_evaluations
            WHERE focus_list_passed = 1
              AND eval_date = {date_sql}
            GROUP BY focus_team_id, focus_stance
            ORDER BY focus_team_id, n DESC
        """
        return query_research_db(sql)
    return query_research_db(
        """
        SELECT focus_team_id, focus_stance, COUNT(*) AS n
        FROM scanner_evaluations
        WHERE focus_list_passed = 1 AND eval_date = ?
        GROUP BY focus_team_id, focus_stance
        ORDER BY focus_team_id, n DESC
        """,
        params=(eval_date,),
    )
