"""Performance — portfolio KPIs, NAV vs SPY trajectory, alpha stats.

Top-line summary tiles + the NAV chart (with uptime-incident markers)
+ alpha-day breakdown + alpha-distribution histogram.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

from charts.nav_chart import make_alpha_histogram, make_nav_chart
from loaders.s3_loader import load_uptime_history
from shared import load_and_prepare_eod

_UPTIME_WINDOW_SESSIONS = 20

st.title("Performance")
st.caption(
    "Phase 2 baseline. Tracked, not optimized — Phase 3 turns on tuning "
    "once the substrate is trustworthy."
)

prep = load_and_prepare_eod()
if prep is None:
    st.warning("Portfolio data temporarily unavailable. Please check back later.")
    st.stop()

# ---------------------------------------------------------------------------
# KPI tiles
# ---------------------------------------------------------------------------

st.markdown("### Portfolio Snapshot")
st.caption(f"As of {prep.perf_date}")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Inception", prep.inception_date.strftime("%b %d, %Y"))
col2.metric("Portfolio NAV", f"${prep.nav:,.0f}")
col3.metric(
    "Cumulative Alpha",
    f"{prep.cumulative_alpha_bps:+.0f} bps",
    delta="vs S&P 500",
    delta_color="off",
)
col4.metric("Alpha Days", f"{prep.up_days} ▲  {prep.down_days} ▼")

st.divider()

# ---------------------------------------------------------------------------
# NAV vs SPY
# ---------------------------------------------------------------------------

uptime_history = load_uptime_history(max_sessions=_UPTIME_WINDOW_SESSIONS)

st.markdown("### Portfolio vs S&P 500")
st.caption(f"As of {prep.perf_date}")
fig_nav = make_nav_chart(prep.eod, uptime_records=uptime_history)
st.plotly_chart(fig_nav, width="stretch")
st.caption(
    "Vertical amber lines mark days with major executor incidents "
    "(≥10% downtime or ≥5 service restarts). Reliability is tracked on "
    "the Uptime page."
)

st.divider()

# ---------------------------------------------------------------------------
# Alpha stats
# ---------------------------------------------------------------------------

st.markdown("### Alpha Performance")
st.caption(f"As of {prep.perf_date}")

win_rate = (prep.up_days / prep.total_days * 100) if prep.total_days > 0 else 0
avg_up_bps = (
    prep.eod_active.loc[prep.eod_active["daily_alpha"] > 0, "daily_alpha"].mean()
    * 10_000
    if prep.up_days > 0
    else 0
)
avg_down_bps = (
    prep.eod_active.loc[prep.eod_active["daily_alpha"] < 0, "daily_alpha"].mean()
    * 10_000
    if prep.down_days > 0
    else 0
)

col_a, col_b, col_c, col_d = st.columns(4)
col_a.metric("Win Rate", f"{win_rate:.1f}%")
col_b.metric("Avg Up-Alpha Day", f"+{avg_up_bps:.0f} bps")
col_c.metric("Avg Down-Alpha Day", f"{avg_down_bps:.0f} bps")
col_d.metric("Trading Days", f"{prep.total_days}")

fig_alpha = make_alpha_histogram(prep.eod)
st.plotly_chart(fig_alpha, width="stretch")
