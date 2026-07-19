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

from charts.nav_chart import make_intraday_curve
from components.morning_brief_card import render_morning_brief_card
from loaders.s3_loader import (
    load_intraday_nav,
    load_intraday_nav_series,
    load_intraday_working_orders,
    load_thesis_summaries,
    load_trades_full,
)
from shared import (
    build_intraday_curve,
    compute_live_metrics,
    load_and_prepare_eod,
    series_date_for,
)
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


def _render_working_orders():
    """Render the daemon's currently-working broker orders, if any."""
    payload = load_intraday_working_orders() or {}
    orders = [o for o in (payload.get("open_orders") or []) if o.get("is_working")]
    if not orders:
        st.caption("No orders working at the broker right now.")
        return
    rows = [
        {
            "Ticker": o.get("ticker"),
            "Action": o.get("action"),
            "Remaining": pd.to_numeric(o.get("remaining"), errors="coerce"),
            "Type": o.get("order_type") or "—",
            "Limit": pd.to_numeric(o.get("limit_price"), errors="coerce"),
            "Status": o.get("status") or "—",
        }
        for o in orders
    ]
    st.caption(
        f"🔵 {len(orders)} order{'s' if len(orders) != 1 else ''} working at the broker"
    )
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={
            "Remaining": st.column_config.NumberColumn("Remaining", format="%d"),
            "Limit": st.column_config.NumberColumn("Limit", format="dollar"),
        },
    )


def _render_live_header(m):
    """Render the live intraday header strip (NAV + today's return + alpha)."""
    st.markdown(f"#### 🟢 Live — as of {m.as_of_et}")
    cols = st.columns(3)
    cols[0].metric("Live NAV", f"${m.nav:,.0f}", delta=f"{m.day_return:+.2%} today")
    cols[1].metric(
        "S&P 500 — today",
        f"{m.spy_return:+.2%}" if m.spy_return is not None else "—",
    )
    cols[2].metric(
        "Alpha vs S&P 500",
        f"{m.day_alpha:+.2%}" if m.day_alpha is not None else "—",
    )


def _render_intraday_curve(nav_json, prep):
    """Render today's intraday portfolio-vs-SPY cumulative-return curve.

    Needs >=2 points to draw a line; silently renders nothing earlier in
    the session (the header numbers already convey the live state)."""
    series_date = series_date_for(nav_json)
    if not series_date:
        return
    curve = build_intraday_curve(load_intraday_nav_series(series_date), prep)
    if curve is None or len(curve) < 2:
        return
    st.plotly_chart(make_intraday_curve(curve), width="stretch")


st.title("Live Portfolio")

prep = load_and_prepare_eod()
if prep is None:
    st.warning("Portfolio data temporarily unavailable. Please check back later.")
    st.stop()

# Live intraday header — shown only while the daemon is publishing a fresh,
# IB-connected NAV snapshot (i.e. during market hours). Otherwise the page is
# the standard last-close view. Holdings + trades below are always EOD-sourced.
_nav_json = load_intraday_nav()
_live = compute_live_metrics(_nav_json, prep)
if _live is not None:
    _render_live_header(_live)
    _render_intraday_curve(_nav_json, prep)
    _render_working_orders()
    st.divider()
    st.caption(
        "Live figures above update intraday while the market is open. "
        "Holdings and trades below are as of the last close."
    )
else:
    st.caption(
        "Current positions and recent fills from the running system, "
        "as of the last close."
    )

# ---------------------------------------------------------------------------
# Positions snapshot (parsed once; also supplies held tickers to the brief)
# ---------------------------------------------------------------------------

snapshot_raw = prep.latest.get("positions_snapshot", "{}")
if pd.isna(snapshot_raw):
    snapshot_raw = "{}"

try:
    positions = json.loads(snapshot_raw)
except (TypeError, ValueError):
    positions = {}


def _held_tickers(pos) -> set[str]:
    """Held symbols from the positions snapshot (dict-of-ticker or list form)."""
    if isinstance(pos, dict):
        return {str(t) for t in pos.keys() if t and t != "CASH"}
    if isinstance(pos, list):
        return {str(p.get("ticker")) for p in pos if p.get("ticker")}
    return set()


# ---------------------------------------------------------------------------
# Morning Brief (Phase-2 consumer, config#664) — Overview card. Rerun-driven:
# the four-gate cadence inside decides whether to call the brief LLM (OpenRouter
# / DeepSeek V4 Flash) or reuse cache.
# ---------------------------------------------------------------------------
render_morning_brief_card(held_tickers=_held_tickers(positions))

# ---------------------------------------------------------------------------
# Current Holdings
# ---------------------------------------------------------------------------

st.markdown("### Current Holdings")
st.caption(f"As of {prep.perf_date}")

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
            "Shares": pd.to_numeric(info.get("shares"), errors="coerce"),
            "Value": float(mv),
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
            "Shares": pd.to_numeric(p.get("shares"), errors="coerce"),
            "Value": float(mv),
            "Sector": p.get("sector", "—") or "—",
            "Rationale": thesis_by_ticker.get(ticker, "—"),
        })

if rows:
    cash = prep.nav - total_invested
    rows.append({
        "Ticker": "CASH",
        "Shares": None,
        "Value": float(cash),
        "Sector": "—",
    })
    # Numeric dtypes stay numeric so column-header sorting sorts by VALUE,
    # not lexically over pre-formatted strings ("$9,800" > "$12,000" was the
    # bug); display formatting moves to column_config.
    pos_df = pd.DataFrame(rows, columns=["Ticker", "Shares", "Value", "Sector"])
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
        column_config={
            "Shares": st.column_config.NumberColumn("Shares", format="%d"),
            "Value": st.column_config.NumberColumn("Value", format="dollar"),
        },
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

if trades_df is None or trades_df.empty or "created_at" not in trades_df.columns:
    st.info("No recent trades to display.")
    st.stop()

# `date` is the NYSE trading_day the order acted on — strictly
# backward-looking (DATE_CONVENTIONS.md), so a trade filled intraday today
# is stamped with YESTERDAY's session. `created_at` (actual fill timestamp)
# is what "recent" must window/sort/display on here.
rt = trades_df.copy()
rt["_date"] = pd.to_datetime(rt["created_at"], utc=True).dt.date
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

date_col = pd.to_datetime(rt["created_at"], utc=True).dt.strftime("%Y-%m-%d")

display = pd.DataFrame({
    "Date": date_col.values,
    "Ticker": rt["ticker"].values,
})
action_col = next((c for c in ("action", "signal") if c in rt.columns), None)
if action_col:
    display["Action"] = rt[action_col].values
display["Shares"] = shares_num.round().values
display["Value"] = value_num.values

display = display.reset_index(drop=True)
trades_event = st.dataframe(
    display,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="recent_trades_table",
    column_config={
        "Shares": st.column_config.NumberColumn("Shares", format="%d"),
        "Value": st.column_config.NumberColumn("Value", format="dollar"),
    },
)
_maybe_open_detail(
    "trades_detail",
    trades_event.selection.rows,
    display["Ticker"],
    positions,
    trades_df,
)
