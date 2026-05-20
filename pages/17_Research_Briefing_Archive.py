"""
Research Briefing Archive — Alpha Engine (private console)

Per-process artifact archive (ROADMAP Observability Item 5) for the
Research morning briefing — the rendered email content the research
Lambda persists weekly. Latest inline + prior ~2 weeks one click each.
Producer: alpha-engine-research archive/manager.py → consolidated/.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from components.process_archive import ProcessArchiveSpec, render_process_archive


st.set_page_config(
    page_title="Research Briefing Archive — Alpha Engine",
    page_icon="📰",
    layout="wide",
)

st.divider()

render_process_archive(
    ProcessArchiveSpec(
        title="Research Briefing Archive",
        description=(
            "The rendered research morning-briefing email content, as "
            "persisted to s3://alpha-engine-research/consolidated/{date}/"
            "morning.md. Latest run inline; prior runs click-to-expand."
        ),
        list_prefix="consolidated/",
        basename="morning.md",
        reader="markdown",
        empty_message=(
            "No research briefings archived yet "
            "(consolidated/{date}/morning.md)."
        ),
    )
)

