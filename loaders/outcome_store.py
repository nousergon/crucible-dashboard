"""
outcome_store.py — the dashboard's single accessor over the long-format
``score_performance_outcomes`` store (EPIC config#1483, consumer cutover
config#1531).

WHY THIS EXISTS
---------------
The eval horizon was historically encoded in wide, horizon-suffixed
``score_performance`` columns (a ``beat_spy_{h}d`` / ``return_{h}d`` /
``spy_{h}d_return`` family, plus a canonical primary-horizon log-alpha
column). An incomplete horizon rename silently starves consumers for months
(config#1456 root cause). The
root-cause fix makes the horizon a PARAMETER: outcomes live in the
long-format ``score_performance_outcomes`` table (one row per signal ×
horizon; DDL owned by crucible-research ``archive/schema.py``, produced by
alpha-engine-data ``collectors/signal_returns.py``), and consumers filter
``WHERE horizon_days = :h`` with ``:h`` resolved from
``nousergon_lib.quant.horizons.HorizonPolicy``.

This module is the ONE place in this repo that reads that store (M0 contract
discipline). Every dashboard consumer of ``score_performance`` outcome
columns goes through :func:`attach_outcomes`, which re-sources the wide
outcome columns from the long store under the SAME names, so
``loaders/db_loader.py`` and the views/charts downstream do not need to know
the physical storage changed.

UNITS — the one deliberate compatibility conversion
---------------------------------------------------
The long store is CANONICAL and stores returns as DECIMALS (0.043). The
legacy wide columns store them as 2dp-rounded PERCENT (4.30) — a quirk of the
wide producer (``round(x * 100, 2)`` in alpha-engine-data Step 2). Dashboard
consumers (accuracy charts, alpha-distribution histograms, trade-outcome
tables) were built on the percent convention, so :func:`attach_outcomes`
reproduces it EXACTLY (same round-to-2dp) at this single documented
boundary. Consumers that want full-precision decimals should read
:func:`load_outcomes` directly. Do NOT add a second decimal<->percent
conversion anywhere else in this repo (mirrors the precedent set in
crucible-backtester's outcome-store accessor, config#1528).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd
from nousergon_lib.quant.horizons import DEFAULT_POLICY, HorizonPolicy

logger = logging.getLogger(__name__)

_TABLE = "score_performance_outcomes"

# The long-format record fields (mirrors nousergon_lib.contracts
# outcome_record v1 + the producer's physical columns).
_LONG_COLUMNS = (
    "signal_id", "symbol", "score_date", "horizon_days",
    "beat_spy", "stock_return", "spy_return", "log_alpha",
    "is_primary", "resolved_at",
)

# ---------------------------------------------------------------------------
# Policy-derived wide-column NAME constants (not literals) for downstream
# dashboard consumers (charts/accuracy_chart.py, the views listed in
# config#1531). These are the same physical column names attach_outcomes()
# writes back onto a score_performance-shaped frame -- resolved from
# HorizonPolicy so a horizon change is a one-line update HERE, never a
# fleet grep-and-replace of a hardcoded suffix (the config#1483 bug class).
# ---------------------------------------------------------------------------
_PRIMARY_COLS = DEFAULT_POLICY.outcome_columns(DEFAULT_POLICY.primary_horizon)
PRIMARY_HORIZON_DAYS = DEFAULT_POLICY.primary_horizon
BEAT_SPY_PRIMARY = _PRIMARY_COLS.beat_spy
RETURN_PRIMARY = _PRIMARY_COLS.stock_return
SPY_RETURN_PRIMARY = _PRIMARY_COLS.spy_return
LOG_ALPHA_PRIMARY = _PRIMARY_COLS.log_alpha


def store_exists(conn: sqlite3.Connection) -> bool:
    """True iff the long-format store table exists in this research.db."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
    ).fetchone()
    return row is not None


def load_outcomes(
    db: str | Path | sqlite3.Connection,
    policy: HorizonPolicy = DEFAULT_POLICY,
    horizons: tuple[int, ...] | None = None,
) -> pd.DataFrame:
    """Load long-format outcome rows for the policy horizons.

    Args:
        db:       research.db path, or an already-open sqlite3 connection.
        policy:   HorizonPolicy resolving which horizons exist (default: the
                  ratified fleet policy -- primary 21d + diagnostic 5d).
        horizons: optional explicit subset (must be policy horizons); default
                  ``policy.all_horizons``.

    Returns a DataFrame with the long-format columns (returns as DECIMALS --
    the canonical units). An unproduced diagnostic horizon simply yields no
    rows (graceful-empty by design). A missing PRIMARY horizon in a store
    that otherwise has rows is a producer starvation bug -- fail-loud via
    ``policy.require_primary_present``.
    """
    hs = tuple(horizons) if horizons is not None else policy.all_horizons
    unknown = set(hs) - set(policy.all_horizons)
    if unknown:
        raise ValueError(
            f"horizons {sorted(unknown)} are not in the active HorizonPolicy "
            f"{policy.all_horizons} -- a non-policy horizon read is exactly "
            f"the bug class config#1483 exists to kill"
        )

    own_conn = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(str(db)) if own_conn else db
    try:
        if not store_exists(conn):
            # Pre-cutover / freshly-created DB: absent table is detectable,
            # not an exception -- callers gate on emptiness exactly as they
            # gated on an unresolved wide column. WARN so a starved run is
            # loud.
            logger.warning(
                "%s table absent from research.db -- long-format store not "
                "yet populated (producer: alpha-engine-data signal_returns "
                "Step 2c)",
                _TABLE,
            )
            return pd.DataFrame(columns=_LONG_COLUMNS)
        placeholders = ",".join("?" for _ in hs)
        df = pd.read_sql_query(
            f"SELECT {', '.join(_LONG_COLUMNS)} FROM {_TABLE} "
            f"WHERE horizon_days IN ({placeholders}) "
            f"ORDER BY score_date, symbol, horizon_days",
            conn,
            params=tuple(int(h) for h in hs),
        )
    finally:
        if own_conn:
            conn.close()

    if not df.empty and policy.primary_horizon in set(int(h) for h in hs):
        # Fail-loud starvation gate: rows exist but none at the canonical
        # primary horizon means the producer is starving the canonical label.
        policy.require_primary_present(df["horizon_days"].unique())
    return df


def attach_outcomes(
    df: pd.DataFrame,
    db: str | Path | sqlite3.Connection,
    policy: HorizonPolicy = DEFAULT_POLICY,
) -> pd.DataFrame:
    """Re-source a ``score_performance`` DataFrame's outcome columns from the
    long-format store (the config#1531 physical cutover).

    For each policy horizon ``h``, the wide outcome columns named by
    ``policy.outcome_columns(h)`` (beat-SPY flag, stock/SPY returns, and --
    for the primary horizon -- the canonical log-alpha) are DROPPED from
    ``df`` and replaced with values read from ``score_performance_outcomes``,
    joined by ``(symbol, score_date)``. Returns are converted decimal ->
    2dp percent to preserve the legacy wide-column units exactly (see module
    docstring); ``log_alpha`` stays decimal (it always was). Rows without a
    long-store record get NaN -- the same "unresolved" signal consumers
    already gate on.

    Non-outcome columns (scores, stance, sector, price_{h}d, eval_date_{h}d)
    pass through untouched -- they are not part of the outcome contract.

    Coverage guard (fail-loud discipline): while the wide columns still
    exist (dual-write soak, pre-Phase-4), any row that is resolved in the
    wide column but missing from the long store is counted and WARNed -- a
    divergence there means the Phase-2 producer is skipping rows and must be
    investigated, not silently absorbed.
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    key = ["symbol", "score_date"]
    long_df = load_outcomes(db, policy=policy)
    if not long_df.empty:
        long_df = long_df.copy()
        # score_performance loaders sometimes parse score_date to datetime;
        # the store keeps TEXT dates. Normalize the join key to match df's
        # dtype.
        if pd.api.types.is_datetime64_any_dtype(out["score_date"]):
            long_df["score_date"] = pd.to_datetime(long_df["score_date"])

    for h in policy.all_horizons:
        cols = policy.outcome_columns(h)
        is_primary = policy.is_primary(h)
        # (long-store field -> wide column name, decimal->percent?) per
        # horizon.
        mapping = [
            ("beat_spy", cols.beat_spy, False),
            ("stock_return", cols.stock_return, True),
            ("spy_return", cols.spy_return, True),
        ]
        if is_primary:
            mapping.append(("log_alpha", cols.log_alpha, False))

        h_rows = (
            long_df[long_df["horizon_days"] == h]
            if not long_df.empty
            else pd.DataFrame(columns=_LONG_COLUMNS)
        )

        # Coverage guard against the still-present wide columns (soak
        # window).
        gate_col = cols.beat_spy
        if gate_col in out.columns and not out[gate_col].isna().all():
            wide_resolved = out.loc[out[gate_col].notna(), key]
            if h_rows.empty:
                missing = len(wide_resolved)
            else:
                merged_check = wide_resolved.merge(
                    h_rows[key].drop_duplicates(), on=key, how="left", indicator=True
                )
                missing = int((merged_check["_merge"] == "left_only").sum())
            if missing:
                logger.warning(
                    "outcome_store divergence: %d row(s) resolved in the "
                    "wide %dd columns but ABSENT from %s -- the Phase-2 "
                    "producer is skipping rows; investigate before trusting "
                    "this cycle (config#1483)",
                    missing, h, _TABLE,
                )

        # Drop the wide-sourced columns, then join the long-sourced ones in
        # their place under the same (policy-derived) names.
        out = out.drop(columns=[w for _, w, _p in mapping if w in out.columns])
        if h_rows.empty:
            for _, wide_name, _p in mapping:
                # float64 NaN, not pd.NA -- assigning pd.NA to a fresh column
                # infers an `object` dtype that a later float64 astype()
                # cannot safely cast on pandas 3.x (`TypeError: float()
                # argument ... not 'NAType'`). np.nan constructs the column
                # as float64 directly, matching every other numeric column
                # attach_outcomes produces.
                out[wide_name] = float("nan")
            continue

        # Dedup on the join key before attaching: the table's DB-level
        # uniqueness constraint is (signal_id, horizon_days), not
        # (symbol, score_date, horizon_days) -- signal_id is
        # f"{symbol}:{score_date}" by construction in today's sole producer,
        # so the two are equivalent in practice, but that is an invariant of
        # the producer, not a schema guarantee this reader can rely on. A
        # future backfill / correction / second producer writing a second
        # signal_id for the same (symbol, score_date, horizon) would
        # otherwise fan the merge out silently (1 input row -> 2 output rows
        # for that signal in every downstream chart), with no error/warning.
        # keep="last" takes the most-recently-resolved row (ORDER BY
        # score_date, symbol, horizon_days in load_outcomes is not a
        # resolution-order guarantee, so this is a deliberate, documented
        # choice, not implicit row-order luck).
        h_rows_dedup = h_rows.drop_duplicates(subset=key, keep="last")
        attach = h_rows_dedup[key + [f for f, _, _p in mapping]].rename(
            columns={f: w for f, w, _p in mapping}
        )
        for field, wide_name, to_percent in mapping:
            if to_percent:
                # EXACT legacy convention: the wide producer stored
                # round(decimal * 100, 2). Reproducing the rounding keeps
                # column-level parity byte-identical during the soak.
                attach[wide_name] = (attach[wide_name] * 100).round(2)
        out = out.merge(attach, on=key, how="left")

    return out
