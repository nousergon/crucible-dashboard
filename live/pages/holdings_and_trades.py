"""Holdings & Trades — current positions plus the last 5 trading days of fills.

Default landing page for live.nousergon.ai. Positions parse out of the
EOD `positions_snapshot` field; trades come from `trades_full`. Both
read-only.
"""

import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import load_trades_full
from shared import load_and_prepare_eod

_RECENT_TRADES_WINDOW_DAYS = 5

st.title("Holdings & Trades")

prep = load_and_prepare_eod()
if prep is None:
    st.warning("Portfolio data temporarily unavailable. Please check back later.")
    st.stop()

# ---------------------------------------------------------------------------
# Current Holdings
# ---------------------------------------------------------------------------

st.markdown("### Current Holdings")
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

st.divider()

# ---------------------------------------------------------------------------
# Recent Trades
# ---------------------------------------------------------------------------

st.markdown("### Recent Trades")

trades_df = load_trades_full()
if trades_df is None or trades_df.empty or "date" not in trades_df.columns:
    st.info("No recent trades to display.")
    st.stop()

rt = trades_df.copy()
rt["_date"] = pd.to_datetime(rt["date"]).dt.date
recent_dates = sorted(rt["_date"].dropna().unique(), reverse=True)[:_RECENT_TRADES_WINDOW_DAYS]
rt = rt[rt["_date"].isin(recent_dates)].sort_values("_date", ascending=False, kind="stable")

if rt.empty:
    st.info("No trades in the last 5 trading days.")
    st.stop()

st.caption(
    f"Last {len(recent_dates)} trading day"
    f"{'s' if len(recent_dates) != 1 else ''} "
    f"({recent_dates[-1]:%Y-%m-%d} → {recent_dates[0]:%Y-%m-%d})"
)

shares_num = pd.to_numeric(rt.get("filled_shares"), errors="coerce").fillna(
    pd.to_numeric(rt.get("shares"), errors="coerce")
)
fill_price = pd.to_numeric(rt.get("fill_price"), errors="coerce")
order_price = pd.to_numeric(rt.get("price_at_order"), errors="coerce")
price_used = fill_price.fillna(order_price)
value_num = shares_num * price_used

date_col = pd.to_datetime(rt["date"]).dt.strftime("%Y-%m-%d")

display = pd.DataFrame({
    "Date": date_col.values,
    "Ticker": rt["ticker"].values,
})
action_col = next((c for c in ("action", "signal") if c in rt.columns), None)
if action_col:
    display["Action"] = rt[action_col].values
display["Shares"] = [
    f"{int(round(x))}" if pd.notna(x) else "—" for x in shares_num
]
display["Value"] = [
    f"${v:,.0f}" if pd.notna(v) else "—" for v in value_num
]

st.dataframe(display.reset_index(drop=True), width="stretch", hide_index=True)
