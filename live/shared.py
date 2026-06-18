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


def _prior_close(prep: EodPrep, live_date) -> Optional[tuple[float, Optional[float]]]:
    """Return ``(base_nav, base_spy)`` from the last EOD row STRICTLY before
    ``live_date`` — the baseline today's live figures are measured against.

    Strictly-prior auto-handles the post-reconcile window where today's row
    already exists (we measure against yesterday, not today's own freshly
    booked close). ``base_spy`` is None when that row lacks a usable
    spy_close. Returns None when there's no prior close or NAV is unusable.
    """
    prior = prep.eod[prep.eod["date"].dt.date < live_date]
    if prior.empty:
        return None
    base = prior.iloc[-1]
    base_nav = pd.to_numeric(base.get("portfolio_nav"), errors="coerce")
    if not base_nav or base_nav <= 0:
        return None
    base_spy = pd.to_numeric(base.get("spy_close"), errors="coerce")
    base_spy = float(base_spy) if base_spy and base_spy > 0 else None
    return float(base_nav), base_spy


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

    live_date = ts.astimezone(_ET).date()
    base = _prior_close(prep, live_date)
    if base is None:
        return None
    base_nav, base_spy = base
    day_return = float(nav) / base_nav - 1.0

    spy_return: Optional[float] = None
    day_alpha: Optional[float] = None
    spy_last = nav_json.get("spy_last")
    if spy_last and base_spy:
        spy_return = float(spy_last) / base_spy - 1.0
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


def series_date_for(nav_json: Optional[dict]) -> Optional[str]:
    """ET trading-date (``YYYY-MM-DD``) of a nav.json snapshot — the
    nav_series key to fetch for the intraday curve. None if unparseable."""
    if not nav_json:
        return None
    ts = _parse_iso_utc(nav_json.get("timestamp", ""))
    if ts is None:
        return None
    return ts.astimezone(_ET).date().isoformat()


def build_intraday_curve(
    series_json: Optional[dict],
    prep: Optional[EodPrep],
) -> Optional[pd.DataFrame]:
    """Build the intraday portfolio-vs-SPY cumulative-return frame.

    Rebases the daemon's nav_series points to the prior EOD close so the
    chart shows today's % return path for both the portfolio and SPY.
    Returns a DataFrame with columns ``time`` (ET, tz-naive), ``port_cum``
    and ``spy_cum`` (percent points), or None if not renderable. ``spy_cum``
    is all-NA when the prior close lacks a usable SPY mark.
    """
    if not series_json or prep is None:
        return None
    points = series_json.get("points")
    if not isinstance(points, list) or not points:
        return None

    rows = []
    for p in points:
        if not isinstance(p, dict):
            continue
        t = _parse_iso_utc(p.get("t", ""))
        nav = p.get("nav")
        if t is None or nav is None:
            continue
        rows.append((t.astimezone(_ET).replace(tzinfo=None), float(nav), p.get("spy")))
    if not rows:
        return None

    # Baseline date: the producer-stamped trading_day, else the last point.
    td = series_json.get("trading_day")
    try:
        live_date = pd.Timestamp(td).date() if td else rows[-1][0].date()
    except (ValueError, TypeError):
        live_date = rows[-1][0].date()

    base = _prior_close(prep, live_date)
    if base is None:
        return None
    base_nav, base_spy = base

    df = pd.DataFrame(rows, columns=["time", "nav", "spy"])
    df["port_cum"] = (df["nav"] / base_nav - 1.0) * 100.0
    if base_spy:
        spy = pd.to_numeric(df["spy"], errors="coerce")
        df["spy_cum"] = (spy / base_spy - 1.0) * 100.0
    else:
        df["spy_cum"] = pd.NA
    return df[["time", "port_cum", "spy_cum"]]
