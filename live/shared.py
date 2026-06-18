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
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from loaders.s3_loader import load_eod_pnl

_ET = ZoneInfo("America/New_York")

# A nav.json older than this is treated as stale → no live header. The daemon
# poll interval is 60s; 5 min tolerates a few missed ticks / clock skew while
# still hiding the strip promptly once the daemon stops at EOD.
_LIVE_STALENESS_SECONDS = 300


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
# Live intraday metrics (derived from the daemon's intraday/nav.json snapshot)
# ---------------------------------------------------------------------------


@dataclass
class LiveMetrics:
    """Today's live portfolio numbers, derived from intraday/nav.json.

    The producer publishes RAW marks (NAV + SPY last); all derivation
    happens here against the prior EOD close so the display convention can
    change without redeploying the trading box.
    """

    nav: float
    day_return: float                 # fraction, e.g. 0.012 = +1.2%
    day_alpha: Optional[float]        # fraction vs SPY; None if SPY mark absent
    spy_return: Optional[float]       # fraction
    as_of_et: str                     # "1:24 PM ET"
    age_seconds: float


def _parse_iso_utc(ts: str) -> Optional[datetime]:
    """Parse the daemon's ``...Z``-suffixed ISO timestamp as tz-aware UTC."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def compute_live_metrics(
    nav_json: Optional[dict],
    prep: Optional[EodPrep],
    *,
    now_utc: Optional[datetime] = None,
) -> Optional[LiveMetrics]:
    """Derive today's live NAV / return / alpha, or None if not live.

    Returns None (→ caller shows the standard EOD view) when any of:
    the snapshot is missing, IB is disconnected, the snapshot is stale
    (> 5 min old), NAV is absent, or there is no prior EOD close to
    measure today against (e.g. the snapshot's trading date is already
    booked in eod_pnl, which happens after the EOD reconcile runs).
    """
    if not nav_json or prep is None:
        return None
    if not nav_json.get("ib_connected"):
        return None

    nav = nav_json.get("net_liquidation")
    if nav is None:
        return None

    ts = _parse_iso_utc(nav_json.get("timestamp", ""))
    if ts is None:
        return None
    now = now_utc or datetime.now(timezone.utc)
    age = (now - ts).total_seconds()
    if age < 0 or age > _LIVE_STALENESS_SECONDS:
        return None

    # Baseline = last EOD close STRICTLY before the snapshot's trading date.
    # This auto-handles the post-reconcile window where today's row already
    # exists (then there's no prior-day baseline mismatch — we measure
    # against yesterday, not against today's own freshly-booked close).
    live_date = ts.astimezone(_ET).date()
    eod = prep.eod
    prior = eod[eod["date"].dt.date < live_date]
    if prior.empty:
        return None
    base = prior.iloc[-1]

    base_nav = pd.to_numeric(base.get("portfolio_nav"), errors="coerce")
    if not base_nav or base_nav <= 0:
        return None
    day_return = float(nav) / float(base_nav) - 1.0

    spy_return: Optional[float] = None
    day_alpha: Optional[float] = None
    base_spy = pd.to_numeric(base.get("spy_close"), errors="coerce")
    spy_last = nav_json.get("spy_last")
    if spy_last and base_spy and base_spy > 0:
        spy_return = float(spy_last) / float(base_spy) - 1.0
        day_alpha = day_return - spy_return

    as_of_et = ts.astimezone(_ET).strftime("%-I:%M %p ET")

    return LiveMetrics(
        nav=float(nav),
        day_return=day_return,
        day_alpha=day_alpha,
        spy_return=spy_return,
        as_of_et=as_of_et,
        age_seconds=age,
    )
