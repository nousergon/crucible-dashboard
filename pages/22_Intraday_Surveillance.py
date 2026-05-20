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

from loaders.s3_loader import load_intraday_heartbeat, load_intraday_latest_prices


st.set_page_config(
    page_title="Intraday Surveillance — Alpha Engine",
    page_icon="📡",
    layout="wide",
)

st.divider()

st.markdown("### Intraday Surveillance")
st.caption(
    "The every-30-min intraday alerts process is Telegram-only — it "
    "persists no per-run S3 artifact, so there is no dated archive to "
    "show (unlike the other Item-5 processes). Surfaced instead: the "
    "daemon-published surveillance snapshots the alerts Lambda consumes "
    "each run. Per-alert findings live in the Telegram surveillance "
    "channel."
)
st.divider()

heartbeat = load_intraday_heartbeat()
prices = load_intraday_latest_prices()

st.markdown("#### Daemon heartbeat — `intraday/heartbeat.json`")
if heartbeat:
    st.json(heartbeat)
else:
    st.info(
        "No heartbeat snapshot available — the daemon publishes this "
        "during market hours; expected empty outside the trading window "
        "or pre-deploy."
    )

st.divider()

st.markdown("#### Latest IB snapshot prices — `intraday/latest_prices.json`")
if prices:
    st.json(prices)
else:
    st.info(
        "No latest-prices snapshot available — daemon-published during "
        "market hours."
    )

