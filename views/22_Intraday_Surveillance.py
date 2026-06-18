"""
Intraday Surveillance — Alpha Engine (private console)

ROADMAP Observability Item 5, process (f): the every-30-min intraday
research alerts. Unlike the other five email-emitting processes, this
one persists NO per-run artifact — findings are emitted as a Telegram
silent-rollup digest only (alpha-engine-research lambda/alerts_handler.py).

There is therefore no dated artifact archive to render. Honest surface
instead: the daemon-published surveillance snapshots the alerts Lambda
consumes each run (intraday/heartbeat.json liveness +
intraday/latest_prices.json IB snapshot). This documents the
no-persisted-artifact reality rather than fabricating a history that
does not exist.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from charts.nav_chart import make_intraday_curve
from intraday_live import build_intraday_curve, compute_live_metrics, series_date_for
from loaders.s3_loader import (
    load_eod_pnl,
    load_intraday_heartbeat,
    load_intraday_latest_prices,
    load_intraday_nav,
    load_intraday_nav_series,
)


st.divider()

st.markdown("### Intraday Surveillance")
st.caption(
    "Live portfolio state from the running daemon (NAV + today's "
    "return/alpha + the intraday curve), plus the raw surveillance "
    "snapshots the every-30-min alerts Lambda consumes each run. The "
    "alerts process itself is Telegram-only — per-alert findings live in "
    "the Telegram surveillance channel."
)
st.divider()

# ── Live portfolio (intraday) ──────────────────────────────────────────────
# Same daemon artifacts + derivation as live.nousergon.ai (shared
# intraday_live module). Shown only while the daemon publishes a fresh,
# IB-connected snapshot (market hours).
_nav_json = load_intraday_nav()
_eod = load_eod_pnl()
_live = compute_live_metrics(_nav_json, _eod)

if _live is not None:
    st.markdown(f"#### 🟢 Live portfolio — as of {_live.as_of_et}")
    cols = st.columns(3)
    cols[0].metric("Live NAV", f"${_live.nav:,.0f}", delta=f"{_live.day_return:+.2%} today")
    cols[1].metric(
        "S&P 500 — today",
        f"{_live.spy_return:+.2%}" if _live.spy_return is not None else "—",
    )
    cols[2].metric(
        "Alpha vs S&P 500",
        f"{_live.day_alpha:+.2%}" if _live.day_alpha is not None else "—",
    )
    _curve = build_intraday_curve(load_intraday_nav_series(series_date_for(_nav_json)), _eod)
    if _curve is not None and len(_curve) >= 2:
        st.plotly_chart(make_intraday_curve(_curve), use_container_width=True)
else:
    st.info(
        "No live portfolio snapshot — the daemon publishes `intraday/nav.json` "
        "during market hours. Expected empty outside the trading window or "
        "before the daemon next runs on this code."
    )

st.divider()

# ── Raw surveillance snapshots ──────────────────────────────────────────────
with st.expander("Raw daemon snapshots — heartbeat + latest IB prices", expanded=False):
    heartbeat = load_intraday_heartbeat()
    prices = load_intraday_latest_prices()

    st.markdown("**Daemon heartbeat** — `intraday/heartbeat.json`")
    if heartbeat:
        st.json(heartbeat)
    else:
        st.info(
            "No heartbeat snapshot available — the daemon publishes this "
            "during market hours; expected empty outside the trading window "
            "or pre-deploy."
        )

    st.markdown("**Latest IB snapshot prices** — `intraday/latest_prices.json`")
    if prices:
        st.json(prices)
    else:
        st.info(
            "No latest-prices snapshot available — daemon-published during "
            "market hours."
        )

