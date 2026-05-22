"""Uptime — pipeline reliability KPI.

Reads the rolling uptime history (most recent N sessions) and renders
the public uptime panel: headline % + per-session bar + breakdown.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

from components.uptime_kpi import render_uptime_kpi
from loaders.s3_loader import load_uptime_history

_UPTIME_WINDOW_SESSIONS = 20

st.title("Uptime")
st.caption(
    "Phase 2 primary KPI. Tracks pipeline reliability — \"is the system "
    "running?\" — across the most recent trading sessions."
)

uptime_history = load_uptime_history(max_sessions=_UPTIME_WINDOW_SESSIONS)
render_uptime_kpi(uptime_history)
