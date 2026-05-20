"""
Predictor Training Archive — Alpha Engine (private console)

Per-process artifact archive (ROADMAP Observability Item 5) for the
weekly predictor training email. The training summary JSON (accuracy
metrics, gate verdicts, model version) is the source the email is
rendered from; the email body itself is not separately persisted.
Producer: alpha-engine-predictor training/train_handler.py →
predictor/metrics/training_summary_{date}.json. Weekly cadence → ~8
runs ≈ 2 months retained.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from components.process_archive import ProcessArchiveSpec, render_process_archive


st.set_page_config(
    page_title="Predictor Training Archive — Alpha Engine",
    page_icon="🏋️",
    layout="wide",
)

st.divider()

render_process_archive(
    ProcessArchiveSpec(
        title="Predictor Training Archive",
        description=(
            "Weekly predictor retrain summary — accuracy metrics, "
            "promotion-gate verdicts, model version — the source the "
            "training email is rendered from. "
            "s3://alpha-engine-research/predictor/metrics/"
            "training_summary_{date}.json."
        ),
        list_prefix="predictor/metrics/training_summary_",
        suffix=".json",
        reader="json",
        retention_days=8,  # weekly producer
        empty_message=(
            "No training summaries archived yet "
            "(predictor/metrics/training_summary_{date}.json)."
        ),
    )
)

