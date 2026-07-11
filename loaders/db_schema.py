"""Canonical column contract for the research.db tables the dashboard reads.

WHY THIS EXISTS (config#963 — schema-drift resilience)
------------------------------------------------------
``loaders/db_loader.py`` reads research.db with SQL whose projections
enumerate column names as bare string literals (``"SELECT ticker,
cio_decision, cio_rank, ..."``). When a producer-side column is renamed,
those literals silently rot: ``query_research_db`` catches the resulting
``OperationalError``, logs a truncated warning, and returns an EMPTY frame —
so the page renders blank/zeroed instead of failing loud. The same class of
break already bit us once through the JSON side (``charts/
attribution_chart.py`` defaulting every bar to 0.0 after an upstream shape
change, PR #281 / config#1481).

This module is the SINGLE SOURCE OF TRUTH for the SQL field names the
dashboard depends on, so a producer rename is a one-line edit HERE instead of
a grep across ``db_loader.py``'s enumerated SELECTs. It mirrors the precedent
already ratified for the horizon-suffixed outcome columns
(``loaders/outcome_store.py`` deriving names from
``nousergon_lib.quant.horizons.HorizonPolicy`` + the CI burndown ratchet in
``tests/test_wide_horizon_column_burndown_guard.py``, EPIC config#1483): names
flow from ONE declaration, and drift is caught in CI rather than at runtime.

SOURCE OF TRUTH (producer DDL)
------------------------------
The research.db schema is owned by crucible-research ``archive/schema.py`` and
populated by alpha-engine-data collectors. This module declares the SUBSET of
columns the dashboard reads BY NAME (the enumerated projections — SELECT-*
reads carry no literals and need no contract). ``tests/test_db_schema.py``
builds an in-memory research.db FROM this contract and runs every enumerated
``db_loader`` query against it, so a query that references a column absent from
this contract fails CI loud instead of silently returning an empty frame.

SCOPE
-----
Only the tables whose ``db_loader`` reads enumerate column literals are
contracted here: ``scanner_evaluations``, ``team_candidates``,
``cio_evaluations``, ``team_inputs``, and ``predictor_outcomes`` /
``predictor_outcomes_shadow`` (identical shape). The JSON/dict-key read side
(views/charts over S3 artifacts) is a separate, larger burn-down tracked by
config#963 and is intentionally NOT in this module.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-table canonical column contracts.
#
# Each tuple is the set of columns the dashboard reads BY NAME from that table
# (union of every enumerated SELECT / WHERE / ORDER BY / GROUP BY reference in
# db_loader.py). Order within a projection is preserved by the *_COLS group
# tuples below; these table-level tuples are the drift contract the CI pin test
# builds its in-memory schema from.
# ---------------------------------------------------------------------------

SCANNER_EVALUATIONS: tuple[str, ...] = (
    "ticker",
    "eval_date",
    "sector",
    "tech_score",
    "scan_path",
    "quant_filter_pass",
    "liquidity_pass",
    "volatility_pass",
    "balance_sheet_pass",
    "filter_fail_reason",
    "rsi_14",
    "atr_pct",
    "price_vs_ma200",
    "current_price",
    "avg_volume_20d",
    "focus_score",
    "focus_stance",
    "focus_team_id",
    "focus_rank_in_team",
    "focus_rank_in_sector",
    "focus_list_passed",
    "agent_override",
    # v23 (config#750): feature-detected via PRAGMA — projected as NULL on a
    # pre-v23 DB, so it is part of the contract but never required to exist.
    "override_team_id",
)

TEAM_CANDIDATES: tuple[str, ...] = (
    "ticker",
    "eval_date",
    "team_id",
    "team_recommended",
    "quant_rank",
    "quant_score",
    "qual_score",
    "rsi_sub_score",
    "macd_sub_score",
    "ma50_sub_score",
    "ma200_sub_score",
    "momentum_sub_score",
)

CIO_EVALUATIONS: tuple[str, ...] = (
    "ticker",
    "eval_date",
    "team_id",
    "cio_decision",
    "cio_rank",
    "cio_conviction",
    "final_score",
    "quant_score",
    "qual_score",
    "combined_score",
    "macro_shift",
    "rationale",
    "rule_tags",
)

TEAM_INPUTS: tuple[str, ...] = (
    "ticker",
    "eval_date",
    "team_id",
    "source",
    "sector",
)

# predictor_outcomes and predictor_outcomes_shadow share this shape; the
# realized-scorecard / realized-alpha-series queries enumerate exactly these.
PREDICTOR_OUTCOMES: tuple[str, ...] = (
    "model_version",
    "prediction_date",
    "p_up",
    "actual_log_alpha",
    "correct",
)

# The tables whose db_loader reads enumerate column literals — the CI pin test
# iterates this mapping to build its contract-derived in-memory schema.
CONTRACT: dict[str, tuple[str, ...]] = {
    "scanner_evaluations": SCANNER_EVALUATIONS,
    "team_candidates": TEAM_CANDIDATES,
    "cio_evaluations": CIO_EVALUATIONS,
    "team_inputs": TEAM_INPUTS,
    "predictor_outcomes": PREDICTOR_OUTCOMES,
    "predictor_outcomes_shadow": PREDICTOR_OUTCOMES,
}


# ---------------------------------------------------------------------------
# Named projections used by db_loader.py. Each is the exact column list a query
# selects, in order. Building the SELECT clause from these (via ``join``) makes
# a rename a one-line edit here and lets the CI pin test prove every projected
# name is declared in the table contract above.
# ---------------------------------------------------------------------------

CIO_FUNNEL_COLS: tuple[str, ...] = (
    "ticker", "cio_decision", "cio_rank", "cio_conviction", "final_score",
)
CIO_INPUTS_COLS: tuple[str, ...] = (
    "team_id", "ticker", "quant_rank", "quant_score", "qual_score",
)
CIO_EVALUATIONS_COLS: tuple[str, ...] = (
    "ticker", "team_id", "quant_score", "qual_score", "combined_score",
    "macro_shift", "final_score", "cio_decision", "cio_conviction",
    "cio_rank", "rationale", "rule_tags",
)
TEAM_CANDIDATES_COLS: tuple[str, ...] = (
    "ticker", "quant_rank", "quant_score", "qual_score", "team_recommended",
    "rsi_sub_score", "macd_sub_score", "ma50_sub_score", "ma200_sub_score",
    "momentum_sub_score",
)
TEAM_INPUTS_COLS: tuple[str, ...] = ("ticker", "source", "sector")
SCANNER_SCREEN_COLS: tuple[str, ...] = (
    "ticker", "sector", "tech_score", "scan_path", "quant_filter_pass",
    "liquidity_pass", "volatility_pass", "balance_sheet_pass",
    "filter_fail_reason", "rsi_14", "atr_pct", "price_vs_ma200",
    "current_price", "avg_volume_20d",
)
# Focus-list audit: override_team_id is appended by db_loader as either
# "override_team_id" or "NULL AS override_team_id" (v23 feature-detect), so it
# is NOT part of this fixed projection — the remaining columns are.
FOCUS_AUDIT_COLS: tuple[str, ...] = (
    "ticker", "eval_date", "sector", "focus_score", "focus_stance",
    "focus_team_id", "focus_rank_in_team", "focus_rank_in_sector",
    "focus_list_passed", "agent_override",
)
PREDICTOR_SCORECARD_COLS: tuple[str, ...] = (
    "model_version", "prediction_date", "p_up", "actual_log_alpha", "correct",
)
PREDICTOR_REALIZED_SERIES_COLS: tuple[str, ...] = (
    "model_version", "prediction_date", "actual_log_alpha",
)

# Which table each named projection reads, so ``join`` can validate every
# projected name against that table's contract.
_PROJECTION_TABLE: dict[int, str] = {}


def _register(projection: tuple[str, ...], table: str) -> tuple[str, ...]:
    _PROJECTION_TABLE[id(projection)] = table
    return projection


for _proj, _tbl in (
    (CIO_FUNNEL_COLS, "cio_evaluations"),
    (CIO_INPUTS_COLS, "team_candidates"),
    (CIO_EVALUATIONS_COLS, "cio_evaluations"),
    (TEAM_CANDIDATES_COLS, "team_candidates"),
    (TEAM_INPUTS_COLS, "team_inputs"),
    (SCANNER_SCREEN_COLS, "scanner_evaluations"),
    (FOCUS_AUDIT_COLS, "scanner_evaluations"),
    (PREDICTOR_SCORECARD_COLS, "predictor_outcomes"),
    (PREDICTOR_REALIZED_SERIES_COLS, "predictor_outcomes"),
):
    _register(_proj, _tbl)


# Every named projection, for the CI pin test to enumerate.
PROJECTIONS: dict[str, tuple[str, ...]] = {
    "CIO_FUNNEL_COLS": CIO_FUNNEL_COLS,
    "CIO_INPUTS_COLS": CIO_INPUTS_COLS,
    "CIO_EVALUATIONS_COLS": CIO_EVALUATIONS_COLS,
    "TEAM_CANDIDATES_COLS": TEAM_CANDIDATES_COLS,
    "TEAM_INPUTS_COLS": TEAM_INPUTS_COLS,
    "SCANNER_SCREEN_COLS": SCANNER_SCREEN_COLS,
    "FOCUS_AUDIT_COLS": FOCUS_AUDIT_COLS,
    "PREDICTOR_SCORECARD_COLS": PREDICTOR_SCORECARD_COLS,
    "PREDICTOR_REALIZED_SERIES_COLS": PREDICTOR_REALIZED_SERIES_COLS,
}


def projection_table(projection: tuple[str, ...]) -> str | None:
    """Return the table a registered projection reads (None if unregistered)."""
    return _PROJECTION_TABLE.get(id(projection))


def join(projection: tuple[str, ...]) -> str:
    """Build a ``SELECT`` column list from a named projection.

    Every name in *projection* must be declared in its table's contract; an
    undeclared name raises ``ValueError`` at call time (catches a typo or a
    projection that drifted away from the contract before it can silently
    return an empty frame at runtime). Returns the comma+space-joined column
    list — byte-identical to the literal it replaces.
    """
    table = _PROJECTION_TABLE.get(id(projection))
    if table is not None:
        contract = CONTRACT[table]
        unknown = [c for c in projection if c not in contract]
        if unknown:
            raise ValueError(
                f"projection references columns {unknown} not in the "
                f"{table!r} contract (db_schema.CONTRACT) — update the "
                f"contract if the producer added them, or fix the typo"
            )
    return ", ".join(projection)


def warn_missing(df: pd.DataFrame, table: str, *required: str) -> pd.DataFrame:
    """Emit a structured schema-drift WARNING for any *required* column absent
    from *df* (a frame just read from *table*). Returns *df* unchanged — this
    is a fail-soft observability hook, never a behaviour change, so it can be
    dropped into any read path. A non-empty frame missing a contracted column
    is exactly the silent-drift signature this issue exists to surface.
    """
    if df is None or df.empty:
        return df
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning(
            "schema drift: %s returned rows but is missing expected column(s) "
            "%s — a producer rename likely broke a hardcoded read (config#963). "
            "Present columns: %s",
            table, missing, list(df.columns),
        )
    return df
