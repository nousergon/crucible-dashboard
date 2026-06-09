"""Live Portfolio — current positions plus the last 5 trading days of fills.

Default landing page for live.nousergon.ai ("Live Portfolio" titling per
the public-presence plan §9b.1, L4570e). Positions parse out of the EOD
`positions_snapshot` field; trades come from `trades_full`. Both read-only.
"""

import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import load_thesis_summaries, load_trades_full
from shared import load_and_prepare_eod
from ticker_detail import show_ticker_detail


def _maybe_open_detail(state_key: str, selection_rows, ticker_series, positions, trades_df):
    """Open the per-ticker modal for a single-row dataframe selection.

    Guarded on a per-table session key so dismissing the modal (which
    reruns with the selection still set) doesn't immediately reopen it.
    Re-selecting a different row reopens; the CASH row routes to the
    modal's operator-reserved stub rather than being silently inert."""
    if not selection_rows:
        return
    ticker = str(ticker_series.iloc[selection_rows[0]])
    if st.session_state.get(state_key) == ticker:
        return
    st.session_state[state_key] = ticker
    show_ticker_detail(ticker, positions, trades_df)

_RECENT_TRADES_WINDOW_DAYS = 5

st.title("Live Portfolio")
st.caption("Current positions and recent fills from the running system, as of the last close.")

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

thesis_by_ticker = load_thesis_summaries()
# Loaded up-front so the per-ticker modal (opened from either table) can
# surface this ticker's recent fills.
trades_df = load_trades_full()

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
            "Rationale": thesis_by_ticker.get(ticker, "—"),
        })
elif isinstance(positions, list) and positions:
    for p in positions:
        ticker = p.get("ticker", "?")
        mv = p.get("market_value", 0) or 0
        total_invested += mv
        rows.append({
            "Ticker": ticker,
            "Shares": p.get("shares", "—"),
            "Value": f"${mv:,.0f}",
            "Sector": p.get("sector", "—") or "—",
            "Rationale": thesis_by_ticker.get(ticker, "—"),
        })

if rows:
    cash = prep.nav - total_invested
    rows.append({
        "Ticker": "CASH",
        "Shares": "—",
        "Value": f"${cash:,.0f}",
        "Sector": "—",
    })
    pos_df = pd.DataFrame(rows, columns=["Ticker", "Shares", "Value", "Sector"])
    pos_df["Shares"] = pos_df["Shares"].astype(str)
    # Rationale + full per-ticker context now live in the click-through
    # modal (ROADMAP L176) — the table stays scan-friendly. Select a row to
    # open it; CASH routes to the modal's operator-reserved stub.
    st.caption("Select a row to view the full thesis, predictor read, and recent fills for that ticker.")
    holdings_event = st.dataframe(
        pos_df,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="holdings_table",
    )
    _maybe_open_detail(
        "holdings_detail",
        holdings_event.selection.rows,
        pos_df["Ticker"],
        positions,
        trades_df,
    )
else:
    st.info("No open positions.")

st.divider()

# ---------------------------------------------------------------------------
# Recent Trades
# ---------------------------------------------------------------------------

st.markdown("### Recent Trades")

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

display = display.reset_index(drop=True)
trades_event = st.dataframe(
    display,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="recent_trades_table",
)
_maybe_open_detail(
    "trades_detail",
    trades_event.selection.rows,
    display["Ticker"],
    positions,
    trades_df,
)
