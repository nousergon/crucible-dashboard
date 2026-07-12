"""FastAPI app for the Crucible /dash frontend (config#1973, phase 9-B).

Read-only. Response shapes are the view-model's own dict/list outputs —
documented by the contract tests in tests/test_dash_api.py, which the
Next.js client's fixtures mirror. Errors never fabricate data: an absent
upstream artifact yields the view-model's honest-ABSENT rows, and a hard
loader failure returns 503 with the exception name (fail-loud, never a
silent empty 200).

This 503 contract depends on the loader layer actually raising instead of
swallowing operational failures to None/fallback — see loaders/s3_loader.py
S3AccessError + raise_s3_access_errors() (config#2339). Before that fix, a
non-NoSuchKey S3 ClientError (most notably AccessDenied — this box's
most-recurring failure class) was indistinguishable from honest-ABSENT by
the time it reached `_guard` below, so an IAM gap rendered as a 200 with
"no data" instead of a 503. `_guard` opts every loader call into strict
mode; the ~40 Streamlit console call sites into the same shared loaders do
NOT opt in, so their existing degrade-to-ABSENT + get_recent_s3_errors()
telemetry (console views 2/6) is unaffected by this contract.
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
    load_intraday_nav,
    load_intraday_nav_series,
    load_report_card,
    raise_s3_access_errors,
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
    """Run a loader/builder; convert hard failures to 503 (never empty 200).

    Runs the loader under raise_s3_access_errors() (config#2339) so a
    non-NoSuchKey S3 ClientError (e.g. AccessDenied) surfaces as
    S3AccessError instead of being swallowed to None deep in the loader —
    otherwise this except clause would never see it and an IAM-gap failure
    would render as an honest-looking 200 "no data" response.
    """
    try:
        with raise_s3_access_errors():
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


@app.get("/api/intraday")
def intraday() -> list[dict]:
    """Today's intraday portfolio-vs-SPY return path (the 1D chart range).

    Daemon-published best-effort artifact, market-hours only — an empty list
    outside sessions is the honest state, not an error (the frontend says
    "no intraday session data" rather than rendering a blank chart as if it
    were a flat day).
    """
    import intraday_live

    nav = _guard(load_intraday_nav)
    if not nav:
        return []
    day = intraday_live.series_date_for(nav)
    if not day:
        return []
    series = _guard(load_intraday_nav_series, day)
    frame = intraday_live.build_intraday_curve(series, _guard(load_eod_pnl))
    if frame is None or frame.empty:
        return []
    frame = frame.copy()
    frame["time"] = frame["time"].astype(str)
    out = frame.rename(columns={"port_cum": "Portfolio", "spy_cum": "SPY"}).round(4)
    return [
        {k: (None if v != v else v) for k, v in row.items()}  # NaN → null (json-safe)
        for row in out.to_dict(orient="records")
    ]
