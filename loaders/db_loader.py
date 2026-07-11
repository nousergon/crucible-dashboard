"""
SQLite research.db loader for the Alpha Engine Dashboard.
Downloads research.db from S3 to /tmp and queries it via sqlite3.
"""

import logging
import sqlite3
import os

import pandas as pd
import streamlit as st

from loaders.outcome_store import attach_outcomes
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

    The wide horizon-suffixed outcome columns (beat-SPY flag, stock/SPY
    returns, and the canonical primary-horizon log-alpha, for both the
    primary and diagnostic horizons) are re-sourced from the long-format
    score_performance_outcomes store via loaders.outcome_store.attach_outcomes
    (EPIC config#1483 Phase 3, config#1531) -- filtered by
    nousergon_lib.quant.horizons.HorizonPolicy, not hardcoded horizon-suffix
    literals. Values are unchanged (percent units preserved) so downstream
    consumers need no changes.
    """
    sql = f"SELECT * FROM score_performance ORDER BY score_date DESC LIMIT {_MAX_QUERY_ROWS}"
    df = _normalize_score_col(query_research_db(sql))
    if not df.empty:
        df = df.sort_values("score_date", ascending=True).reset_index(drop=True)
        conn = load_research_db()
        if conn is not None:
            df = attach_outcomes(df, conn)
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

    The outcome columns for every policy horizon (beat-SPY flag, stock/SPY
    returns, and the canonical primary-horizon log-alpha) are re-sourced
    from the long-format score_performance_outcomes store (EPIC config#1483
    Phase 3, config#1531) via loaders.outcome_store, filtered by
    nousergon_lib.quant.horizons.HorizonPolicy, instead of a hardcoded wide
    SELECT that previously fetched only the primary (21d) columns.
    ``symbol`` is still read directly from score_performance since the long
    store carries no non-outcome columns.
    """
    sql = """
        SELECT symbol, score_date, score
        FROM score_performance
        WHERE symbol = ?
        ORDER BY score_date
    """
    df = _normalize_score_col(query_research_db(sql, params=(symbol,)))
    if not df.empty:
        conn = load_research_db()
        if conn is not None:
            df = attach_outcomes(df, conn)
        df = df.drop(columns=["symbol"])
    return df


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


# ---------------------------------------------------------------------------
# Agent-decision review (L4567 Phase 3) — read the decision audit trail the
# research pipeline writes every cycle (scanner_evaluations / team_candidates
# / cio_evaluations / investment_thesis). Mirrors alpha-engine-research's
# scripts/decision_review.py query+funnel logic for the console UI.
# ---------------------------------------------------------------------------

# Canonical "this CIO decision admits the ticker" set — match BOTH "ADVANCE"
# (rubric) and "ADVANCE_FORCED" (floor-fill), or forced entrants read as
# rejections. Mirrors graph.state_schemas.ADVANCE_DECISIONS.
_ADVANCE_DECISIONS = {"ADVANCE", "ADVANCE_FORCED"}


def get_decision_eval_dates(limit: int = 30) -> list[str]:
    """Distinct eval_dates with recorded decisions, most recent first."""
    df = query_research_db(
        "SELECT DISTINCT eval_date FROM scanner_evaluations "
        "ORDER BY eval_date DESC LIMIT ?",
        params=(limit,),
    )
    if df.empty or "eval_date" not in df.columns:
        return []
    return df["eval_date"].astype(str).tolist()


def get_ticker_decision(ticker: str, eval_date: str) -> dict[str, pd.DataFrame]:
    """Everything the pipeline recorded about ``ticker`` on ``eval_date``:
    scanner / team_candidates / cio / thesis frames (each possibly empty)."""
    t = (ticker or "").upper()
    return {
        "scanner": query_research_db(
            "SELECT * FROM scanner_evaluations WHERE ticker=? AND eval_date=?",
            params=(t, eval_date),
        ),
        "team_candidates": query_research_db(
            "SELECT * FROM team_candidates WHERE ticker=? AND eval_date=? "
            "ORDER BY quant_rank",
            params=(t, eval_date),
        ),
        "cio": query_research_db(
            "SELECT * FROM cio_evaluations WHERE ticker=? AND eval_date=?",
            params=(t, eval_date),
        ),
        "thesis": query_research_db(
            "SELECT * FROM investment_thesis WHERE symbol=? AND date=? "
            "ORDER BY run_time DESC LIMIT 1",
            params=(t, eval_date),
        ),
    }


def get_cycle_funnel(eval_date: str) -> dict:
    """Funnel counts for one cycle: scanned→passed, ranked→recommended,
    cio evaluated→advanced, plus the advanced rows."""
    scanned = query_research_db(
        "SELECT COUNT(*) AS n, SUM(quant_filter_pass) AS passed "
        "FROM scanner_evaluations WHERE eval_date=?",
        params=(eval_date,),
    )
    team = query_research_db(
        "SELECT COUNT(*) AS n, SUM(team_recommended) AS rec "
        "FROM team_candidates WHERE eval_date=?",
        params=(eval_date,),
    )
    cio = query_research_db(
        "SELECT ticker, cio_decision, cio_rank, cio_conviction, final_score "
        "FROM cio_evaluations WHERE eval_date=? ORDER BY cio_rank",
        params=(eval_date,),
    )

    def _int(df, col):
        if df.empty or col not in df.columns or pd.isna(df.iloc[0][col]):
            return 0
        return int(df.iloc[0][col])

    advanced = cio
    if not cio.empty and "cio_decision" in cio.columns:
        advanced = cio[
            cio["cio_decision"].astype(str).str.upper().isin(_ADVANCE_DECISIONS)
        ]
    return {
        "eval_date": eval_date,
        "scanner_screened": _int(scanned, "n"),
        "scanner_passed": _int(scanned, "passed"),
        "team_ranked": _int(team, "n"),
        "team_recommended": _int(team, "rec"),
        "cio_evaluated": 0 if cio.empty else len(cio),
        "cio_advanced": 0 if (isinstance(advanced, pd.DataFrame) and advanced.empty) else len(advanced),
        "advanced": advanced,
        "cio_all": cio,
    }


def get_cio_inputs(eval_date: str) -> pd.DataFrame:
    """The candidate set handed TO the CIO for ``eval_date`` — the union of
    every sector team's recommendations (``team_candidates`` rows with
    ``team_recommended=1``). One row per (team_id, ticker) with the team-stage
    scores the CIO sees. Empty frame if no cycle ran / table missing."""
    return query_research_db(
        "SELECT team_id, ticker, quant_rank, quant_score, qual_score "
        "FROM team_candidates "
        "WHERE eval_date=? AND team_recommended=1 "
        "ORDER BY team_id, quant_rank",
        params=(eval_date,),
    )


def get_cio_evaluations(eval_date: str) -> pd.DataFrame:
    """All CIO decisions for ``eval_date`` — every ticker the CIO evaluated,
    its source team, blended scores, decision, conviction, rank, rationale and
    rule_tags. Ordered ADVANCEd-first (by rank), then the rest. Empty frame if
    the CIO stage didn't run / persist that cycle."""
    return query_research_db(
        "SELECT ticker, team_id, quant_score, qual_score, combined_score, "
        "macro_shift, final_score, cio_decision, cio_conviction, cio_rank, "
        "rationale, rule_tags "
        "FROM cio_evaluations WHERE eval_date=? "
        "ORDER BY (cio_rank IS NULL), cio_rank",
        params=(eval_date,),
    )


def get_team_candidates(eval_date: str, team_id: str) -> pd.DataFrame:
    """One sector team's ranked candidates for a cycle — quant rank/score, qual
    score, the per-sub-signal scores, and the team_recommended flag. Ordered by
    quant_rank. Empty frame if the team didn't run / persist that cycle."""
    return query_research_db(
        "SELECT ticker, quant_rank, quant_score, qual_score, team_recommended, "
        "rsi_sub_score, macd_sub_score, ma50_sub_score, ma200_sub_score, "
        "momentum_sub_score "
        "FROM team_candidates WHERE eval_date=? AND team_id=? "
        "ORDER BY (quant_rank IS NULL), quant_rank",
        params=(eval_date, team_id),
    )


def get_team_inputs(eval_date: str, team_id: str) -> pd.DataFrame:
    """The complete candidate set HANDED to one sector team for a cycle, from
    the team_inputs ledger (research.db schema v19). Columns: ticker, source
    ('scanner' | 'held_population'), sector. Empty frame when the table is
    absent (pre-v19 DB) or the cycle predates the ledger — callers fall back to
    team_candidates in that case."""
    return query_research_db(
        "SELECT ticker, source, sector FROM team_inputs "
        "WHERE eval_date=? AND team_id=? ORDER BY ticker",
        params=(eval_date, team_id),
    )


def get_scanner_evaluations(eval_date: str) -> pd.DataFrame:
    """The full scanner screen for a cycle (~900 names) from scanner_evaluations:
    per-gate pass flags (quant_filter_pass / liquidity_pass / volatility_pass /
    balance_sheet_pass), filter_fail_reason, scan_path, sector, and the
    technical indicators. Empty frame if no cycle ran that date."""
    return query_research_db(
        "SELECT ticker, sector, tech_score, scan_path, quant_filter_pass, "
        "liquidity_pass, volatility_pass, balance_sheet_pass, filter_fail_reason, "
        "rsi_14, atr_pct, price_vs_ma200, current_price, avg_volume_20d "
        f"FROM scanner_evaluations WHERE eval_date=? "
        f"ORDER BY sector, tech_score DESC LIMIT {_MAX_QUERY_ROWS}",
        params=(eval_date,),
    )


def explain_why_not(ticker: str, eval_date: str) -> dict:
    """Walk the decision funnel and report where ``ticker`` was dropped.

    Returns ``{ticker, eval_date, stage, verdict}`` where ``stage`` is one of
    ``no_record`` / ``scanner`` / ``team`` / ``cio`` / ``chosen``. Mirrors the
    CLI's funnel logic (recognizes ADVANCE_FORCED as chosen)."""
    t = (ticker or "").upper()
    rec = get_ticker_decision(t, eval_date)
    scanner, teams, cio = rec["scanner"], rec["team_candidates"], rec["cio"]
    s = None if scanner.empty else scanner.iloc[0]

    if scanner.empty and teams.empty and cio.empty:
        return {"ticker": t, "eval_date": eval_date, "stage": "no_record",
                "verdict": (f"No decision record for {t} on {eval_date} — not in the "
                            f"screened universe that cycle, or no cycle ran on this date.")}

    # Stage 1 — scanner quant filter.
    if s is not None and not int(s.get("quant_filter_pass") or 0):
        reason = s.get("filter_fail_reason") or "below_thresholds"
        gates = {g: s.get(g) for g in ("liquidity_pass", "volatility_pass", "balance_sheet_pass")}
        failed = [g for g, v in gates.items() if v == 0]
        return {"ticker": t, "eval_date": eval_date, "stage": "scanner",
                "verdict": (f"Dropped at the SCANNER stage: filter_fail_reason={reason}"
                            + (f"; failed gates: {', '.join(failed)}" if failed else "")
                            + f" (tech_score={s.get('tech_score')}).")}

    # Stage 2 — sector-team quant ranking.
    if not teams.empty:
        recommended = teams[teams["team_recommended"] == 1] if "team_recommended" in teams.columns else teams.iloc[0:0]
        if recommended.empty:
            t0 = teams.iloc[0]
            tid = t0.get("team_id")
            ctx = query_research_db(
                "SELECT ticker, quant_score FROM team_candidates "
                "WHERE team_id=? AND eval_date=? AND team_recommended=1 ORDER BY quant_rank",
                params=(tid, eval_date),
            )
            recs = [] if ctx.empty else ctx["ticker"].tolist()
            smin = None if ctx.empty else ctx["quant_score"].min()
            smax = None if ctx.empty else ctx["quant_score"].max()
            return {"ticker": t, "eval_date": eval_date, "stage": "team",
                    "verdict": (f"Screened by the '{tid}' team but NOT recommended "
                                f"(team_recommended=0): quant_rank={t0.get('quant_rank')}, "
                                f"quant_score={t0.get('quant_score')}, qual_score={t0.get('qual_score')}. "
                                f"The team recommended {len(recs)} pick(s) "
                                f"({', '.join(recs) or 'none'}) with quant_score {smin}–{smax}.")}
    elif s is not None and int(s.get("quant_filter_pass") or 0):
        return {"ticker": t, "eval_date": eval_date, "stage": "team",
                "verdict": (f"Passed the scanner quant filter but did not appear in any "
                            f"sector team's ranked picks — the team's quant analyst screened "
                            f"it out without surfacing it.")}

    # Stage 3 — CIO.
    if not cio.empty:
        c = cio.iloc[0]
        decision = str(c.get("cio_decision") or "").upper()
        if decision in _ADVANCE_DECISIONS:
            return {"ticker": t, "eval_date": eval_date, "stage": "chosen",
                    "verdict": (f"{t} WAS chosen: CIO decision={decision}, rank={c.get('cio_rank')}, "
                                f"conviction={c.get('cio_conviction')}, final_score={c.get('final_score')}. "
                                f"Rationale: {c.get('rationale') or '(none recorded)'}")}
        return {"ticker": t, "eval_date": eval_date, "stage": "cio",
                "verdict": (f"Reached the CIO but was not advanced: decision={decision or '(none)'}, "
                            f"final_score={c.get('final_score')}. "
                            f"Rationale: {c.get('rationale') or '(none recorded)'}")}

    return {"ticker": t, "eval_date": eval_date, "stage": "no_record",
            "verdict": (f"{t} was recommended by its team on {eval_date} but has no CIO row — "
                        f"the CIO stage may not have run, or it wasn't persisted.")}


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


def _per_spec_realized_series(df: pd.DataFrame, stage: str, window: int) -> pd.DataFrame:
    """Per-spec rolling realized-α series from a resolved-outcomes frame.

    For each ``model_version`` (the "spec"), computes the per-date mean realized
    21d log-alpha (the daily realized edge of that spec's picks), then a rolling
    mean over ``window`` prediction-dates so the operator sees the trajectory,
    not the per-date noise. Returns a long frame: model_version, stage,
    prediction_date, realized_alpha (per-date mean), rolling_realized_alpha,
    n_predictions (that date). Empty frame in → empty frame out.
    """
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["actual_log_alpha"] = pd.to_numeric(d["actual_log_alpha"], errors="coerce")
    d["model_version"] = d["model_version"].fillna("champion-legacy")
    d = d.dropna(subset=["actual_log_alpha"])
    if d.empty:
        return pd.DataFrame()

    rows = []
    for version, g in d.groupby("model_version"):
        # Per-date mean realized alpha (cross-sectional average of that spec's
        # resolved picks on each date), ordered by date for the rolling window.
        per_date = (
            g.groupby("prediction_date")["actual_log_alpha"]
            .agg(realized_alpha="mean", n_predictions="size")
            .reset_index()
            .sort_values("prediction_date")
        )
        # min_periods=1 so the series starts immediately and ramps into the
        # full window — an empty leaderboard week shouldn't blank the chart.
        per_date["rolling_realized_alpha"] = (
            per_date["realized_alpha"].rolling(window=window, min_periods=1).mean()
        )
        per_date.insert(0, "stage", stage)
        per_date.insert(0, "model_version", version)
        rows.append(per_date)
    return pd.concat(rows, ignore_index=True)


def get_per_spec_realized_alpha_series(window: int = 8) -> pd.DataFrame:
    """Per-spec rolling realized-α series for the model-zoo noise-monitor panel.

    Companion to ``get_model_version_scorecard`` (the point-in-time table):
    this returns the *trajectory* — per (model_version, prediction_date) mean
    realized 21d log-alpha plus a ``window``-date rolling mean — so the Model
    Zoo page can chart each spec's realized edge over time next to the rotation
    leaderboard. Observability-only (config#1079): the relative-best promotion
    ranks by leak-free CPCV mean IC, so this is a noise monitor, not a gate.

    Unions champion outcomes (``predictor_outcomes``) with challenger outcomes
    (``predictor_outcomes_shadow``). Columns: model_version, stage,
    prediction_date, realized_alpha, rolling_realized_alpha, n_predictions —
    sorted by model_version then prediction_date. Empty until outcomes mature;
    a missing shadow table degrades to champion-only.
    """
    cols = "model_version, prediction_date, actual_log_alpha"
    live = query_research_db(
        f"SELECT {cols} FROM predictor_outcomes WHERE actual_log_alpha IS NOT NULL"
    )
    shadow = query_research_db(
        f"SELECT {cols} FROM predictor_outcomes_shadow WHERE actual_log_alpha IS NOT NULL"
    )
    parts = [
        _per_spec_realized_series(live, "champion", window),
        _per_spec_realized_series(shadow, "challenger", window),
    ]
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(
        ["model_version", "prediction_date"]
    ).reset_index(drop=True)


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
#
# v23 (config#750) added override_team_id so overrides can be attributed to the
# team whose quant agent reached outside its focus list, instead of collapsing
# into one anonymous focus_team_id=NULL group. The loaders below detect the
# column and gracefully fall back to the legacy (unattributed) grouping on a
# pre-v23 DB so the page never breaks between the migration merging and the
# next Saturday SF run repopulating research.db.


def _scanner_eval_columns() -> set[str]:
    """Column names present on scanner_evaluations (empty set if unavailable).

    Used to feature-detect the v23 override_team_id column so queries degrade
    gracefully on a research.db that predates config#750.
    """
    info = query_research_db("PRAGMA table_info(scanner_evaluations)")
    if info.empty or "name" not in info.columns:
        return set()
    return set(info["name"].astype(str))


def get_focus_list_audit(
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Per-ticker focus_list audit rows from scanner_evaluations.

    Returns the canonical audit columns: ticker, eval_date, sector,
    focus_score, focus_stance, focus_team_id, focus_rank_in_team,
    focus_rank_in_sector, focus_list_passed, agent_override,
    override_team_id, quant_filter_pass. Optional date range filter on
    eval_date.

    override_team_id (v23, config#750) names which team's quant agent reached
    outside its focus list for an override row; NULL for focus-list members /
    non-override rows, and projected as NULL on a pre-v23 DB.

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
    # Feature-detect the v23 column so the SELECT never fails on a pre-config#750
    # research.db — project a NULL placeholder there so the column always exists
    # downstream (the view/Rows-tab can reference it unconditionally).
    override_team_col = (
        "override_team_id"
        if "override_team_id" in _scanner_eval_columns()
        else "NULL AS override_team_id"
    )
    sql = f"""
        SELECT
            ticker, eval_date, sector,
            focus_score, focus_stance, focus_team_id,
            focus_rank_in_team, focus_rank_in_sector,
            focus_list_passed, agent_override,
            {override_team_col},
            quant_filter_pass
        FROM scanner_evaluations
        {where_sql}
        ORDER BY eval_date DESC, focus_team_id, focus_rank_in_team
        LIMIT {_MAX_QUERY_ROWS}
    """
    return query_research_db(sql, params=tuple(params) if params else None)


def get_focus_list_weekly_summary() -> pd.DataFrame:
    """Per-week per-team funnel cardinality + override-rate summary.

    Aggregates scanner_evaluations to one row per (eval_date, team), where
    ``team`` is the focus-list team for focus rows and, on a v23+ DB
    (config#750), the OVERRIDING team for override rows — so each team's
    overrides count against that team instead of collapsing into one anonymous
    focus_team_id=NULL ("—") group:
      - focus_team_id      — team identity (focus_team_id, else override_team_id)
      - n_focus_list       — count of focus_list_passed=1
      - n_picks            — count of quant_filter_pass=1 (agent picked)
      - n_overrides        — count of agent_override=1 (this team's overrides)
      - precision          — focus_list_passed=1 AND quant_filter_pass=1
                             / focus_list_passed=1  (was the focus list
                             a good predictor of agent picks?)
      - recall             — focus_list_passed=1 AND quant_filter_pass=1
                             / quant_filter_pass=1  (did the focus list
                             cover the agent's picks?)
      - override_hit_rate  — agent_override=1 AND quant_filter_pass=1
                             / agent_override=1  (when the agent reached
                             outside the focus list, was it right?)

    On a pre-v23 DB the override_team_id column is absent, so grouping falls back
    to focus_team_id alone and overrides keep landing in the legacy NULL group.

    Empty DataFrame when the focus_list columns are absent or no rows have
    focus_list_passed flags set yet (pre-Sat-5/17 SF).
    """
    # Per-team override attribution (config#750): group override rows under the
    # overriding team when the v23 column exists. COALESCE keeps focus rows on
    # focus_team_id and moves override rows (focus_team_id=NULL) onto
    # override_team_id; both fall back to NULL for legacy unattributed rows.
    has_override_team = "override_team_id" in _scanner_eval_columns()
    team_expr = (
        "COALESCE(focus_team_id, override_team_id)"
        if has_override_team
        else "focus_team_id"
    )
    sql = f"""
        SELECT
            eval_date,
            {team_expr} AS focus_team_id,
            SUM(CASE WHEN focus_list_passed = 1 THEN 1 ELSE 0 END) AS n_focus_list,
            SUM(CASE WHEN quant_filter_pass = 1 THEN 1 ELSE 0 END) AS n_picks,
            SUM(CASE WHEN agent_override = 1 THEN 1 ELSE 0 END) AS n_overrides,
            SUM(CASE WHEN focus_list_passed = 1 AND quant_filter_pass = 1 THEN 1 ELSE 0 END) AS n_focus_and_picked,
            SUM(CASE WHEN agent_override = 1 AND quant_filter_pass = 1 THEN 1 ELSE 0 END) AS n_override_and_picked
        FROM scanner_evaluations
        WHERE focus_team_id IS NOT NULL OR agent_override = 1
        GROUP BY eval_date, {team_expr}
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
