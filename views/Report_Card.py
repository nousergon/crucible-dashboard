"""Report Card — the evaluator's institutional grade across all 7 modules.

Reads ``evaluator/{date}/report_card.json`` (the Report Card v2 MetricRecord
substrate produced by the alpha-engine-evaluator grading Lambda). Outcome leads;
the module tiles decompose it.
"""

import streamlit as st

from components.report_card_v2 import render_overview
from loaders.s3_loader import load_report_card

st.title("📋 System Report Card")
st.caption(
    "Institutional grade across Portfolio Outcome + the six component modules. "
    "Each tile rolls up its components under a critical-gate rule (a critical RED "
    "fails the module; a critical N/A holds it at WATCH — never a false GREEN)."
)

render_overview(load_report_card())
