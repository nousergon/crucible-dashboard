"""Shared intraday-live derivation for both dashboards.

Pure logic (no Streamlit, S3, or config) used by BOTH the public live site
(``live/``) and the private console (``views/22_Intraday_Surveillance.py``)
to turn the daemon's raw intraday artifacts into display values:

- ``intraday/nav.json`` — latest (NAV, SPY) snapshot → live header numbers
  (today's return + alpha vs SPY), via :func:`compute_live_metrics`.
- ``intraday/nav_series/{day}.json`` — the day's (NAV, SPY) point series →
  the intraday portfolio-vs-SPY cumulative-return curve, via
  :func:`build_intraday_curve`.

Both derive against the **prior EOD close** (the producer publishes only raw
marks; the display convention lives here so it can change without
redeploying the trading box). Inputs are plain ``eod`` DataFrames with
``date`` / ``portfolio_nav`` / ``spy_close`` columns so either dashboard's
loader can feed it — the live site passes its prepared EOD frame, the
console passes ``load_eod_pnl()`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")

# A nav.json older than this is treated as stale → no live header. The daemon
# poll interval is 60s; 5 min tolerates a few missed ticks / clock skew while
# still hiding the strip promptly once the daemon stops at EOD.
_LIVE_STALENESS_SECONDS = 300


@dataclass
class LiveMetrics:
    """Today's live portfolio numbers, derived from intraday/nav.json.

    The producer publishes RAW marks (NAV + SPY last); all derivation
    happens here against the prior EOD close.
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


def _prior_close(
    eod_df: Optional[pd.DataFrame], live_date
) -> Optional[tuple[float, Optional[float]]]:
    """Return ``(base_nav, base_spy)`` from the last EOD row STRICTLY before
    ``live_date`` — the baseline today's live figures are measured against.

    Strictly-prior auto-handles the post-reconcile window where today's row
    already exists (we measure against yesterday, not today's own freshly
    booked close). ``base_spy`` is None when that row lacks a usable
    spy_close. Returns None when there's no prior close, the frame is
    empty/None, or NAV is unusable.
    """
    if eod_df is None or eod_df.empty or "date" not in eod_df.columns:
        return None
    df = eod_df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    prior = df[df["date"].dt.date < live_date]
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
    eod_df: Optional[pd.DataFrame],
    *,
    now_utc: Optional[datetime] = None,
) -> Optional[LiveMetrics]:
    """Derive today's live NAV / return / alpha, or None if not live.

    Returns None (→ caller shows the standard EOD view) when any of:
    the snapshot is missing, IB is disconnected, the snapshot is stale
    (> 5 min old), NAV is absent, or there is no prior EOD close to measure
    today against.
    """
    if not nav_json or eod_df is None:
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
    base = _prior_close(eod_df, live_date)
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
    eod_df: Optional[pd.DataFrame],
) -> Optional[pd.DataFrame]:
    """Build the intraday portfolio-vs-SPY cumulative-return frame.

    Rebases the daemon's nav_series points to the prior EOD close so the
    chart shows today's % return path for both the portfolio and SPY.
    Returns a DataFrame with columns ``time`` (ET, tz-naive), ``port_cum``
    and ``spy_cum`` (percent points), or None if not renderable. ``spy_cum``
    is all-NA when the prior close lacks a usable SPY mark.
    """
    if not series_json or eod_df is None:
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

    base = _prior_close(eod_df, live_date)
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
