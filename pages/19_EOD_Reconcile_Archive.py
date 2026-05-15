"""
EOD Reconcile Archive — Alpha Engine (private console)

Per-process artifact archive (ROADMAP Observability Item 5) for the
EOD reconciliation email. The executor persists the rendered EOD email
HTML per trading day; this archive surfaces it (the rolling
trades/eod_pnl.csv remains the structured table on the Portfolio page —
not duplicated here).
Producer: alpha-engine executor/eod_emailer.py → consolidated/{date}/eod.html.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from components.header import render_footer, render_header
from components.process_archive import ProcessArchiveSpec, render_process_archive
from components.styles import inject_base_css, inject_docs_css


st.set_page_config(
    page_title="EOD Reconcile Archive — Alpha Engine",
    page_icon="🌙",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_base_css()
inject_docs_css()
render_header(current_page="EOD Reconcile Archive")
st.divider()

render_process_archive(
    ProcessArchiveSpec(
        title="EOD Reconcile Archive",
        description=(
            "The rendered end-of-day reconciliation email (NAV, daily "
            "return vs SPY, alpha) as persisted to "
            "s3://alpha-engine-research/consolidated/{date}/eod.html. "
            "Latest trading day inline; prior ~2 weeks click-to-expand."
        ),
        list_prefix="consolidated/",
        basename="eod.html",
        reader="html",
        empty_message=(
            "No EOD emails archived yet (consolidated/{date}/eod.html)."
        ),
    )
)

render_footer()
