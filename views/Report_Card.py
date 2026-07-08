"""Report Card — the evaluator's institutional grade across all 7 modules.

Reads ``evaluator/{date}/report_card.json`` (the Report Card v2 MetricRecord
substrate produced by the alpha-engine-evaluator grading Lambda). Outcome leads;
the module tiles decompose it.

One page, two views over the same (cached) artifact — the former standalone
Component Detail page is the "Component Detail" view here (console-IA phase 1,
config#1990): every component as a MetricRecord (value, confidence interval,
sample size vs floor, target/red-line, status reason, trend), grouped by tile
and filterable by status. This is where a RED/WATCH on the overview gets
explained — and where each N/A names the producer still to wire.
"""

import streamlit as st

from components.report_card_v2 import render_detail, render_overview
from loaders.s3_loader import load_report_card

st.title("📋 System Report Card")

_view = st.segmented_control(
    "View", ["Overview", "Component Detail"],
    default="Overview", key="report_card_view",
) or "Overview"

card = load_report_card()

if _view == "Overview":
    st.caption(
        "Institutional grade across Portfolio Outcome + the six component modules. "
        "Each tile rolls up its components under a critical-gate rule (a critical RED "
        "fails the module; a critical N/A holds it at WATCH — never a false GREEN)."
    )
    render_overview(card)
else:
    st.caption(
        "Every graded component with its statistical context. Filter to RED + WATCH "
        "to triage, or N/A to see which producers aren't wired yet."
    )
    render_detail(card)
