"""
Nous Ergon — Live Dashboard Overview
https://live.nousergon.ai/

Public read-only dashboard for portfolio performance. Multi-page layout
with native Streamlit sidebar. Marketing/positioning copy lives on the
Astro apex (nousergon.ai); this site is where the charts and tables
live.

Overview page (this file): phase indicator + reliability KPI + system
report card + top-line portfolio metrics. Detail pages: Performance,
Holdings, Trades.
"""

import os
import sys

# live/ has its own loaders/charts/ that shadow the console's top-level
# packages; append the repo root so the shared `components/` widgets
# (phase_indicator, report_card, uptime_kpi) resolve at the top level
# while loaders.* / charts.* still resolve under live/.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from components.phase_indicator import (
    render_phase_indicator,
    render_phase_caption,
    render_phase_descriptions,
)
from components.report_card import render_report_card
from components.uptime_kpi import render_uptime_kpi
from loaders.s3_loader import load_latest_grading, load_uptime_history
from shared import load_and_prepare_eod

_CURRENT_PHASE = "Reliability + Measurability"
_UPTIME_WINDOW_SESSIONS = 20

st.set_page_config(
    page_title="Nous Ergon — Live Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Overview")
st.caption(
    "Phase 2 reliability + measurability snapshot. Use the sidebar for "
    "performance, holdings, and recent trades."
)

render_phase_indicator(current_phase=_CURRENT_PHASE)
render_phase_caption(current_phase=_CURRENT_PHASE)
render_phase_descriptions(current_phase=_CURRENT_PHASE)

st.divider()

uptime_history = load_uptime_history(max_sessions=_UPTIME_WINDOW_SESSIONS)
render_uptime_kpi(uptime_history)

st.divider()

grading = load_latest_grading()
render_report_card(grading)

st.divider()

prep = load_and_prepare_eod()
if prep is None:
    st.warning("Portfolio data temporarily unavailable. Please check back later.")
    st.stop()

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
