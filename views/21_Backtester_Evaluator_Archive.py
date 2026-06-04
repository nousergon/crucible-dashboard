"""
Backtester Evaluator Archive — Alpha Engine (private console)

Per-process artifact archive (ROADMAP Observability Item 5) for the
weekly backtester evaluator email. The reporter persists the rendered
weekly report markdown per run; this archive surfaces it.
Producer: alpha-engine-backtester reporter.py → backtest/{date}/report.md.
Weekly cadence → ~8 runs ≈ 2 months retained.
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
        title="Backtester Evaluator Archive",
        description=(
            "The rendered weekly backtester report (signal-quality, "
            "param sweeps, portfolio simulation, evaluator) as persisted "
            "to s3://alpha-engine-research/backtest/{date}/report.md. "
            "Latest run inline; prior runs click-to-expand."
        ),
        list_prefix="backtest/",
        basename="report.md",
        reader="markdown",
        retention_days=8,  # weekly producer
        empty_message=(
            "No backtester reports archived yet "
            "(backtest/{date}/report.md)."
        ),
    )
)

