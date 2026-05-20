"""
Predictor Briefing Archive — Alpha Engine (private console)

Per-process artifact archive (ROADMAP Observability Item 5) for the
Predictor daily morning briefing. The email body is not separately
persisted — predictions/{date}.json IS the artifact the email is
rendered from, so it is what this archive surfaces.
Producer: alpha-engine-predictor inference/stages/write_output.py.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from components.process_archive import ProcessArchiveSpec, render_process_archive


st.set_page_config(
    page_title="Predictor Briefing Archive — Alpha Engine",
    page_icon="🔮",
    layout="wide",
)

st.divider()

render_process_archive(
    ProcessArchiveSpec(
        title="Predictor Briefing Archive",
        description=(
            "Daily predictor predictions JSON — the artifact the morning "
            "briefing email is rendered from (the email body itself is not "
            "persisted; this is the source of record). "
            "s3://alpha-engine-research/predictor/predictions/{date}.json."
        ),
        list_prefix="predictor/predictions/",
        suffix=".json",
        reader="json",
        empty_message=(
            "No predictor predictions archived yet "
            "(predictor/predictions/{date}.json)."
        ),
    )
)

