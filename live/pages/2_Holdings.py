"""Nous Ergon — Holdings.

Latest portfolio positions parsed from the EOD `positions_snapshot`
field. Adds a synthetic CASH row reflecting `NAV - sum(market_value)`.
"""

import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import streamlit as st

from shared import load_and_prepare_eod

st.set_page_config(
    page_title="Holdings — Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Current Holdings")

prep = load_and_prepare_eod()
if prep is None:
    st.warning("Portfolio data temporarily unavailable. Please check back later.")
    st.stop()

st.caption(f"As of {prep.perf_date}")

snapshot_raw = prep.latest.get("positions_snapshot", "{}")
if pd.isna(snapshot_raw):
    snapshot_raw = "{}"

try:
    positions = json.loads(snapshot_raw)
except (TypeError, ValueError):
    positions = {}

rows: list[dict] = []
total_invested = 0.0
if isinstance(positions, dict) and positions:
    for ticker, info in positions.items():
        mv = info.get("market_value", 0) or 0
        total_invested += mv
        rows.append({
            "Ticker": ticker,
            "Shares": info.get("shares", "—"),
            "Value": f"${mv:,.0f}",
            "Sector": info.get("sector", "—") or "—",
        })
elif isinstance(positions, list) and positions:
    for p in positions:
        mv = p.get("market_value", 0) or 0
        total_invested += mv
        rows.append({
            "Ticker": p.get("ticker", "?"),
            "Shares": p.get("shares", "—"),
            "Value": f"${mv:,.0f}",
            "Sector": p.get("sector", "—") or "—",
        })

if rows:
    cash = prep.nav - total_invested
    rows.append({
        "Ticker": "CASH",
        "Shares": "—",
        "Value": f"${cash:,.0f}",
        "Sector": "—",
    })
    pos_df = pd.DataFrame(rows)
    pos_df["Shares"] = pos_df["Shares"].astype(str)
    st.dataframe(pos_df, width="stretch", hide_index=True)
else:
    st.info("No open positions.")
