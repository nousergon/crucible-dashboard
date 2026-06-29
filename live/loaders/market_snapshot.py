"""Broad-market snapshot for the morning-brief macro lead + materiality gate.

Captures ``{ts, SPY%, QQQ%, VIX}`` — the state the brief LEADS WITH ("why is the
market down today") and the state the cadence's materiality gate (gate 4)
compares against the last brief's snapshot.

Sources, in priority order, all fail-soft (a missing leg → None, never raises):
  * SPY day-return — the live daemon's ``intraday/nav.json`` (``spy_last`` vs
    the prior close, already computed by ``intraday_live.compute_live_metrics``)
    when available; otherwise the 15-min-delayed yfinance quote.
  * QQQ day-return — yfinance (config#664 ADDS QQQ to the live-quote set; the
    daemon snapshot only carries SPY).
  * VIX — yfinance ``^VIX`` level (config#664 ADDS an intraday VIX source for
    the materiality VIX leg; the live site previously had no intraday VIX —
    the console reads VIX from the macro_snapshots DB table, EOD-stale).

yfinance is already a dashboard dependency (``live/loaders/s3_loader.py``
``load_live_day_return``); this reuses that exact 15-min-delayed quote path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from loaders.s3_loader import load_intraday_nav, load_live_day_return

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# yfinance symbol for spot VIX. ^VIX is the CBOE Volatility Index; it quotes
# during market hours like any index (15-min delayed via yfinance fast_info).
VIX_SYMBOL = "^VIX"


def _spy_day_return_pp() -> float | None:
    """SPY intraday day-return in percentage points.

    Prefer the daemon's ``spy_last`` (matches the live header's SPY number, so
    the brief and the header agree); fall back to the yfinance delayed quote
    when the daemon isn't publishing (pre-open / daemon down).
    """
    nav = load_intraday_nav()
    if nav:
        spy_last = nav.get("spy_last")
        spy_base = nav.get("spy_base") or nav.get("spy_prev_close")
        try:
            if spy_last and spy_base and float(spy_base) > 0:
                return (float(spy_last) / float(spy_base) - 1.0) * 100.0
        except (TypeError, ValueError):
            pass
    # Fall back to the delayed yfinance quote (already pp-scaled).
    return load_live_day_return("SPY")


@st.cache_data(ttl=60)
def _vix_level() -> float | None:
    """Spot VIX level (points) from the 15-min-delayed yfinance quote.

    Mirrors ``load_live_day_return``'s yfinance access pattern but returns the
    LEVEL, not a return. Fail-soft: missing yfinance / bad fetch → None (the
    materiality VIX leg then simply can't trip). 60s TTL matches the daemon
    poll cadence used elsewhere on the live site.
    """
    try:
        import yfinance as yf

        fi = yf.Ticker(VIX_SYMBOL).fast_info
        last = getattr(fi, "last_price", None)
        if last and float(last) > 0:
            return float(last)
    except Exception as e:  # noqa: BLE001 — best-effort display/materiality input
        logger.warning("[market_snapshot] VIX fetch failed (%s)", type(e).__name__)
    return None


def capture_market_snapshot() -> dict:
    """Capture the current broad-market snapshot as a plain dict.

    Returns ``{"ts", "spy_day_return_pp", "qqq_day_return_pp", "vix"}`` — the
    shape ``morning_brief_cadence.MarketSnapshot.from_dict`` consumes. ``ts`` is
    tz-aware ET. Any leg may be None when its source is down; the brief and the
    materiality gate both degrade gracefully.
    """
    return {
        "ts": datetime.now(ET).isoformat(),
        "spy_day_return_pp": _spy_day_return_pp(),
        "qqq_day_return_pp": load_live_day_return("QQQ"),
        "vix": _vix_level(),
    }
