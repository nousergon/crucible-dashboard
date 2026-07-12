import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# Focus List moved to the Universe host's Focus Audit tab (same
# scanner_evaluations source as the Funnel); Daily News demoted from a
# standalone nav entry to a tab here — console-IA phase 2b, config#1988.
# Order Book Rationale lives on the Execution front page (executor output).
render_host(
    [
        ("Signals & Research", "2_Signals_and_Research.py"),
        ("Daily News", "Daily_News.py"),
        ("Briefing Archive", "17_Research_Briefing_Archive.py"),
    ],
    key="host_research_signals",
)
