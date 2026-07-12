"""Evaluation — system report card from the weekly evaluator.

Per-module letter grades + sub-component breakdown sourced from
backtest/{date}/grading.json. Complements Uptime: uptime answers \"is
the system running?\", this page answers \"is it running well?\".
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

from components.report_card import render_report_card
from loaders.s3_loader import load_latest_grading

st.title("Evaluation")
st.caption(
    "Per-module letter grades from the weekly evaluator, tracking "
    "directional signal on the Phase-2 substrate."
)

grading = load_latest_grading()
render_report_card(grading)
