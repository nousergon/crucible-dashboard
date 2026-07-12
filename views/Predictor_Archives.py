"""Predictor Archives — raw briefing + training artifacts, one tab.

Consolidates the former Predictor Briefing Archive and Predictor Training
Archive pages (two thin ``render_process_archive`` shells) into a single
Archives tab on the Predictor Detail front page. The artifacts themselves are
already rendered richly on the standalone Predictor page (dated predictions
table, promotion/gate status) — this tab is the raw source-of-record browser.
Console-IA redesign phase 1, alpha-engine-config#1990.

Producers: alpha-engine-predictor ``inference/stages/write_output.py`` (daily
predictions) and ``training/train_handler.py`` (weekly training summary).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from components.process_archive import ProcessArchiveSpec, render_process_archive

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
