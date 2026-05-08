"""
Nous Ergon — Public Portfolio Page
https://nousergon.ai

Layout (top to bottom):
  1. Landing intro — hero + mission + four pillars
  2. Phase indicator + uptime KPI + system report card
  3. Performance — KPI metrics + NAV vs SPY chart + alpha stats
  4. Current Holdings — positions with value
  5. Recent Trades — most-recent-session fills
"""

import json
import os
import sys

# Components live at the project root for sharing with the private dashboard;
# append (don't prepend) so Streamlit's CWD entry (public/) is searched
# first — that keeps `from charts.nav_chart import ...` resolving to
# public/charts/, not the private dashboard's top-level charts/ which
# has a different surface.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st
import yaml

from components.header import render_header, render_footer
from components.landing_intro import render_landing_intro
from components.phase_indicator import (
    render_phase_indicator,
    render_phase_caption,
    render_phase_descriptions,
)
from components.styles import inject_base_css, inject_metric_css
from components.report_card import render_report_card
from components.uptime_kpi import render_uptime_kpi
from loaders.s3_loader import (
    load_eod_pnl,
    load_latest_grading,
    load_trades_full,
    load_uptime_history,
)
from charts.nav_chart import make_nav_chart, make_alpha_histogram

_CURRENT_PHASE = "Reliability + Measurability"
_UPTIME_WINDOW_SESSIONS = 20

# Load config
_config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(_config_path) as _f:
    _cfg = yaml.safe_load(_f)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Shared CSS + Header
# ---------------------------------------------------------------------------

inject_base_css()
inject_metric_css()
render_header(current_page="Home")

# ---------------------------------------------------------------------------
# Landing intro — hero one-liner, mission paragraph, four pillars
# ---------------------------------------------------------------------------

render_landing_intro()

st.divider()

# ---------------------------------------------------------------------------
# Phase Indicator — Phase 2 framing connecting narrative to live receipts
# ---------------------------------------------------------------------------

render_phase_indicator(current_phase=_CURRENT_PHASE)
render_phase_caption(current_phase=_CURRENT_PHASE)
render_phase_descriptions(current_phase=_CURRENT_PHASE)

st.divider()

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

eod = load_eod_pnl()
trades_df = load_trades_full()
uptime_history = load_uptime_history(max_sessions=_UPTIME_WINDOW_SESSIONS)
grading = load_latest_grading()

# ---------------------------------------------------------------------------
# Section 0: Reliability — current-phase primary KPI
# ---------------------------------------------------------------------------

render_uptime_kpi(uptime_history)

st.divider()

# ---------------------------------------------------------------------------
# Section 0.5: System Report Card — structural quality from weekly evaluator
# ---------------------------------------------------------------------------

render_report_card(grading)

st.divider()

if eod is None or eod.empty:
    st.warning("Portfolio data temporarily unavailable. Please check back later.")
    st.stop()

# Parse and prepare
eod["date"] = pd.to_datetime(eod["date"])
eod = eod.sort_values("date").reset_index(drop=True)

eod["port_ret"] = pd.to_numeric(eod["daily_return_pct"], errors="coerce").fillna(0.0) / 100.0
eod["spy_ret"] = pd.to_numeric(eod["spy_return_pct"], errors="coerce").fillna(0.0) / 100.0
eod["daily_alpha"] = pd.to_numeric(eod["daily_alpha_pct"], errors="coerce").fillna(0.0) / 100.0

# Inception date
_inception_override = _cfg.get("inception_date")
if _inception_override:
    inception_date = pd.Timestamp(_inception_override)
    eod = eod[eod["date"] >= inception_date].reset_index(drop=True)
else:
    inception_date = eod["date"].iloc[0]

# Day 0 = inception baseline
eod_active = eod.iloc[1:].reset_index(drop=True) if len(eod) > 1 else eod
latest = eod.iloc[-1]
nav = latest["portfolio_nav"]

# Cumulative returns — direct from NAV and spy_close (no daily chaining)
nav_0 = eod["portfolio_nav"].iloc[0]
eod["port_cum"] = eod["portfolio_nav"] / nav_0 - 1

spy_close = pd.to_numeric(eod.get("spy_close"), errors="coerce")
if spy_close.notna().sum() >= 2:
    spy_0 = spy_close.dropna().iloc[0]
    eod["spy_cum"] = spy_close / spy_0 - 1
    # Forward-fill for any rows missing spy_close
    eod["spy_cum"] = eod["spy_cum"].ffill().fillna(0.0)
else:
    # Fallback to cumprod if spy_close is entirely missing
    eod["spy_cum"] = 0.0
    if len(eod_active) > 0:
        eod_active["spy_cum"] = (1 + eod_active["spy_ret"]).cumprod() - 1
        eod.loc[eod.index[1:], "spy_cum"] = eod_active["spy_cum"].values

cumulative_alpha_bps = (eod["port_cum"].iloc[-1] - eod["spy_cum"].iloc[-1]) * 10_000 if len(eod) > 0 else 0

# Alpha days
up_days = (eod_active["daily_alpha"] > 0).sum()
down_days = (eod_active["daily_alpha"] < 0).sum()
total_days = len(eod_active)

# ===========================================================================
# Section 1: Performance — Secondary KPIs (Phase 3 primary metric)
# ===========================================================================

st.markdown("### Performance — Phase 2 Baseline")
st.caption(
    "Tracked, not optimized. Phase 3 turns on alpha tuning once Phase 2's "
    "substrate is trustworthy."
)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Inception", inception_date.strftime("%b %d, %Y"))
col2.metric("Portfolio NAV", f"${nav:,.0f}")
col3.metric(
    "Cumulative Alpha",
    f"{cumulative_alpha_bps:+.0f} bps",
    delta="vs S&P 500",
    delta_color="off",
)
col4.metric("Alpha Days", f"{up_days} ▲  {down_days} ▼")

# NAV vs SPY chart
_perf_date = eod["date"].iloc[-1].strftime("%Y-%m-%d")
st.markdown("### Portfolio vs S&P 500")
st.caption(
    "Phase 2 baseline trajectory. Phase 3 tuning targets sustained "
    "outperformance vs SPY."
)
st.caption(f"As of {_perf_date}")
fig_nav = make_nav_chart(eod, uptime_records=uptime_history)
st.plotly_chart(fig_nav, width="stretch")
st.caption(
    "Vertical amber lines mark days with major executor incidents "
    f"(≥10% downtime or ≥5 service restarts). Reliability is tracked via "
    "the System Report Card above."
)

# Alpha stats
st.markdown("### Alpha Performance")
st.caption(
    "Phase 2 baseline distribution. Phase 3 tuning targets win rate "
    "and up/down-day asymmetry."
)
st.caption(f"As of {_perf_date}")

col_a, col_b, col_c, col_d = st.columns(4)
win_rate = up_days / total_days * 100 if total_days > 0 else 0
avg_up_bps = eod_active.loc[eod_active["daily_alpha"] > 0, "daily_alpha"].mean() * 10_000 if up_days > 0 else 0
avg_down_bps = eod_active.loc[eod_active["daily_alpha"] < 0, "daily_alpha"].mean() * 10_000 if down_days > 0 else 0

col_a.metric("Win Rate", f"{win_rate:.1f}%")
col_b.metric("Avg Up-Alpha Day", f"+{avg_up_bps:.0f} bps")
col_c.metric("Avg Down-Alpha Day", f"{avg_down_bps:.0f} bps")
col_d.metric("Trading Days", f"{total_days}")

fig_alpha = make_alpha_histogram(eod)
st.plotly_chart(fig_alpha, width="stretch")

st.divider()

# ===========================================================================
# Section 2: Current Holdings
# ===========================================================================

st.markdown("### Current Holdings")
st.caption(f"As of {_perf_date}")

try:
    snapshot_raw = latest.get("positions_snapshot", "{}")
    if pd.isna(snapshot_raw):
        snapshot_raw = "{}"
    positions = json.loads(snapshot_raw)

    rows = []
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
        cash = nav - total_invested
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
except Exception:
    st.info("Position data unavailable.")

st.divider()

# ===========================================================================
# Section 3: Recent Trades — last 5 trading days
# ===========================================================================

_RECENT_TRADES_WINDOW_DAYS = 5

if trades_df is not None and not trades_df.empty and "date" in trades_df.columns:
    rt = trades_df.copy()
    rt["_date"] = pd.to_datetime(rt["date"]).dt.date
    recent_dates = sorted(rt["_date"].dropna().unique(), reverse=True)[:_RECENT_TRADES_WINDOW_DAYS]
    rt = rt[rt["_date"].isin(recent_dates)].sort_values("_date", ascending=False, kind="stable")

    if not rt.empty:
        st.markdown("### Recent Trades")
        st.caption(
            f"Last {len(recent_dates)} trading day"
            f"{'s' if len(recent_dates) != 1 else ''} "
            f"({recent_dates[-1]:%Y-%m-%d} → {recent_dates[0]:%Y-%m-%d})"
        )

        # Shares: prefer filled_shares (intraday daemon writes the actual fill
        # quantity); fall back to ordered `shares` for rows still in flight.
        shares_num = pd.to_numeric(
            rt.get("filled_shares"), errors="coerce"
        ).fillna(pd.to_numeric(rt.get("shares"), errors="coerce"))
        # Value: filled_shares × fill_price, falling back to
        # shares × price_at_order pre-fill.
        fill_price = pd.to_numeric(rt.get("fill_price"), errors="coerce")
        order_price = pd.to_numeric(rt.get("price_at_order"), errors="coerce")
        price_used = fill_price.fillna(order_price)
        value_num = shares_num * price_used

        date_col = pd.to_datetime(rt["date"]).dt.strftime("%Y-%m-%d")

        display = pd.DataFrame({
            "Date": date_col.values,
            "Ticker": rt["ticker"].values,
        })
        action_col = next(
            (c for c in ("action", "signal") if c in rt.columns), None
        )
        if action_col:
            display["Action"] = rt[action_col].values
        display["Shares"] = [
            f"{int(round(x))}" if pd.notna(x) else "—"
            for x in shares_num
        ]
        display["Value"] = [
            f"${v:,.0f}" if pd.notna(v) else "—" for v in value_num
        ]

        st.dataframe(
            display.reset_index(drop=True),
            width="stretch", hide_index=True,
        )
        st.divider()

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

render_footer()
