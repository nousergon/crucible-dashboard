"""Shared data preparation for the live.nousergon.ai dashboard.

Each Streamlit page under `live/` loads + derives the same EOD frame
(cumulative returns, alpha-day counts). Centralizing here keeps the
derivations consistent across Overview, Performance, Holdings, Trades.

`load_and_prepare_eod()` returns None on empty/missing data so pages can
short-circuit with a friendly warning. The underlying loader is cached
via Streamlit, so calling this from every page is cheap.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import yaml

from intraday_live import LiveMetrics, series_date_for  # noqa: F401 (re-exported)
from intraday_live import build_intraday_curve as _il_build_intraday_curve
from intraday_live import compute_live_metrics as _il_compute_live_metrics
from loaders.s3_loader import load_eod_pnl


@dataclass
class EodPrep:
    eod: pd.DataFrame
    eod_active: pd.DataFrame
    latest: pd.Series
    nav: float
    inception_date: pd.Timestamp
    cumulative_alpha_bps: float
    up_days: int
    down_days: int
    total_days: int
    perf_date: str


def _load_inception_override() -> Optional[pd.Timestamp]:
    cfg_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(cfg_path):
        return None
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    override = cfg.get("inception_date")
    return pd.Timestamp(override) if override else None


def load_and_prepare_eod() -> Optional[EodPrep]:
    """Load EOD parquet and derive the columns + scalars every page needs.

    Returns None when no EOD data is available (loader returned empty).
    """
    eod = load_eod_pnl()
    if eod is None or eod.empty:
        return None

    eod = eod.copy()
    eod["date"] = pd.to_datetime(eod["date"])
    eod = eod.sort_values("date").reset_index(drop=True)

    eod["port_ret"] = pd.to_numeric(eod["daily_return_pct"], errors="coerce").fillna(0.0) / 100.0
    eod["spy_ret"] = pd.to_numeric(eod["spy_return_pct"], errors="coerce").fillna(0.0) / 100.0
    eod["daily_alpha"] = pd.to_numeric(eod["daily_alpha_pct"], errors="coerce").fillna(0.0) / 100.0

    inception_override = _load_inception_override()
    if inception_override is not None:
        inception_date = inception_override
        eod = eod[eod["date"] >= inception_date].reset_index(drop=True)
    else:
        inception_date = eod["date"].iloc[0]

    eod_active = eod.iloc[1:].reset_index(drop=True) if len(eod) > 1 else eod
    latest = eod.iloc[-1]
    nav = latest["portfolio_nav"]

    nav_0 = eod["portfolio_nav"].iloc[0]
    eod["port_cum"] = eod["portfolio_nav"] / nav_0 - 1

    spy_close = pd.to_numeric(eod.get("spy_close"), errors="coerce")
    if spy_close.notna().sum() >= 2:
        spy_0 = spy_close.dropna().iloc[0]
        eod["spy_cum"] = (spy_close / spy_0 - 1).ffill().fillna(0.0)
    else:
        eod["spy_cum"] = 0.0
        if len(eod_active) > 0:
            eod_active["spy_cum"] = (1 + eod_active["spy_ret"]).cumprod() - 1
            eod.loc[eod.index[1:], "spy_cum"] = eod_active["spy_cum"].values

    cumulative_alpha_bps = (eod["port_cum"].iloc[-1] - eod["spy_cum"].iloc[-1]) * 10_000

    up_days = int((eod_active["daily_alpha"] > 0).sum())
    down_days = int((eod_active["daily_alpha"] < 0).sum())
    total_days = len(eod_active)

    return EodPrep(
        eod=eod,
        eod_active=eod_active,
        latest=latest,
        nav=float(nav),
        inception_date=inception_date,
        cumulative_alpha_bps=float(cumulative_alpha_bps),
        up_days=up_days,
        down_days=down_days,
        total_days=total_days,
        perf_date=eod["date"].iloc[-1].strftime("%Y-%m-%d"),
    )


# ---------------------------------------------------------------------------
# Live intraday metrics — thin EodPrep-shaped wrappers over the shared
# DataFrame-based logic in intraday_live (also used by the private console).
# ---------------------------------------------------------------------------


def compute_live_metrics(
    nav_json: Optional[dict],
    prep: Optional[EodPrep],
    *,
    now_utc: Optional[datetime] = None,
) -> Optional[LiveMetrics]:
    """Derive today's live NAV / return / alpha, or None if not live.

    Thin wrapper passing ``prep.eod`` to the shared
    :func:`intraday_live.compute_live_metrics`.
    """
    return _il_compute_live_metrics(
        nav_json, prep.eod if prep is not None else None, now_utc=now_utc
    )


def build_intraday_curve(
    series_json: Optional[dict],
    prep: Optional[EodPrep],
) -> Optional[pd.DataFrame]:
    """Build the intraday portfolio-vs-SPY cumulative-return frame.

    Thin wrapper passing ``prep.eod`` to the shared
    :func:`intraday_live.build_intraday_curve`.
    """
    return _il_build_intraday_curve(series_json, prep.eod if prep is not None else None)
