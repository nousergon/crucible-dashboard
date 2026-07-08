"""FastAPI app for the Crucible /dash frontend (config#1973, phase 9-B).

Read-only. Response shapes are the view-model's own dict/list outputs —
documented by the contract tests in tests/test_dash_api.py, which the
Next.js client's fixtures mirror. Errors never fabricate data: an absent
upstream artifact yields the view-model's honest-ABSENT rows, and a hard
loader failure returns 503 with the exception name (fail-loud, never a
silent empty 200).
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query

from loaders.s3_loader import (
    list_backtest_dates,
    load_backtest_file,
    load_eod_pnl,
    load_report_card,
)
from loaders.trust_battery_loader import load_ci_verdicts
from results import view_model as vm
from results.battery_registry import BATTERY_FINDINGS, BATTERY_LEGS

logger = logging.getLogger(__name__)

app = FastAPI(
    title="crucible-dash-api",
    description="Read-only results API for the Crucible /dash surface (plan §9.4).",
    version="0.1.0",
)


def _guard(fn, *args, **kwargs):
    """Run a loader/builder; convert hard failures to 503 (never empty 200)."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — surfaced as an explicit 503, not swallowed
        logger.error("dash-api upstream failure in %s: %s", getattr(fn, "__name__", fn), exc)
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc


def _latest_backtest_date() -> str | None:
    dates = _guard(list_backtest_dates)
    return dates[0] if dates else None


def _bt_json(date: str | None, filename: str) -> dict | None:
    if not date:
        return None
    loaded = _guard(load_backtest_file, date, filename)
    return loaded if isinstance(loaded, dict) else None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "crucible-dash-api"}


@app.get("/api/experiment")
def experiment() -> dict:
    """Identity block — what ran, exactly (§9.3 'About this experiment')."""
    card = _guard(load_report_card)
    date = _latest_backtest_date()
    identity = vm.build_identity(card, date)
    identity["slots"] = [{"slot": s, "impl": i} for s, i in identity["slots"]]
    return identity


@app.get("/api/headline")
def headline() -> list[dict]:
    """Performance stat strip — numbers with provenance, never grades."""
    date = _latest_backtest_date()
    return vm.build_headline(
        _guard(load_eod_pnl),
        _bt_json(date, "metrics.json"),
        _bt_json(date, "portfolio_stats.json"),
    )


@app.get("/api/equity")
def equity() -> list[dict]:
    """Cumulative return series (portfolio vs SPY) since inception."""
    frame = vm.equity_frame(_guard(load_eod_pnl))
    if frame.empty:
        return []
    frame = frame.copy()
    frame["date"] = frame["date"].astype(str)
    return frame.round(4).to_dict(orient="records")


@app.get("/api/alpha-periods")
def alpha_periods(period: str = Query("W", pattern="^[DWM]$")) -> list[dict]:
    """Daily/weekly/monthly alpha dissection (ledger sums, display-level)."""
    frame = vm.alpha_by_period(_guard(load_eod_pnl), period)
    if frame.empty:
        return []
    frame = frame.copy()
    frame["label"] = frame["label"].astype(str)
    return frame.round(4).to_dict(orient="records")


@app.get("/api/attribution")
def attribution() -> list[dict]:
    """Sub-score → outcome attribution with the BH-FDR verdict."""
    return vm.attribution_rows(_bt_json(_latest_backtest_date(), "attribution.json"))


@app.get("/api/integrity")
def integrity() -> list[dict]:
    """The measurement-integrity legs (lookahead audit, adequacy, …)."""
    date = _latest_backtest_date()
    return vm.integrity_rows(
        _bt_json(date, "pit_parity.json"),
        _bt_json(date, "sample_size.json"),
        _bt_json(date, "walk_forward_stability.json"),
        _bt_json(date, "optimizer_churn.json"),
    )


@app.get("/api/verdicts")
def verdicts() -> list[dict]:
    """Grader verdicts scoped to the EXPERIMENT tiles (plan §9.2 — ops
    tiles are structurally unreachable through this API)."""
    return vm.experiment_tile_verdicts(_guard(load_report_card))


@app.get("/api/tiles/{tile_key}")
def tile_detail(tile_key: str) -> dict:
    """Full MetricRecord table for ONE experiment-scoped tile.

    404 for ops tiles by design — the audience split is enforced at the
    API boundary, not left to frontend discipline.
    """
    if tile_key not in vm.EXPERIMENT_TILES and tile_key != "portfolio_outcome":
        raise HTTPException(status_code=404, detail=f"unknown tile {tile_key!r}")
    card = _guard(load_report_card)
    return {"tile": tile_key, "metrics": vm.metric_rows(card, tile_key)}


@app.get("/api/execution")
def execution() -> dict:
    """Execution-sim evidence: headline, triggers, exit rules, shadow book."""
    date = _latest_backtest_date()
    trigger_scorecard = _bt_json(date, "trigger_scorecard.json")
    exit_timing = _bt_json(date, "exit_timing.json")
    shadow_book = _bt_json(date, "shadow_book.json")
    return {
        "headline": vm.execution_headline(trigger_scorecard, exit_timing, shadow_book),
        "triggers": vm.trigger_rows(trigger_scorecard),
        "exit_rules": vm.exit_type_rows(exit_timing),
        "shadow_classification": vm.shadow_classification_rows(shadow_book),
    }


@app.get("/api/trust")
def trust() -> dict:
    """Battery legs with live main-branch CI verdicts + the findings ledger."""
    repos = tuple(sorted({leg["repo"] for leg in BATTERY_LEGS}))
    ci = _guard(load_ci_verdicts, repos)
    return {
        "legs": vm.trust_rows(BATTERY_LEGS, ci),
        "findings": BATTERY_FINDINGS,
    }
