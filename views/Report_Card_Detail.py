"""Report Card — Component Detail.

The operator drill-down: every component as a MetricRecord (value, confidence
interval, sample size vs floor, target/red-line, status reason, trend), grouped
by tile and filterable by status. This is where a RED/WATCH on the overview gets
explained — and where each N/A names the producer still to wire.
"""

import streamlit as st

from components.report_card_v2 import render_detail
from loaders.s3_loader import load_report_card

st.title("🔎 Report Card — Component Detail")
st.caption(
    "Every graded component with its statistical context. Filter to RED + WATCH "
    "to triage, or N/A to see which producers aren't wired yet."
)

render_detail(load_report_card())
