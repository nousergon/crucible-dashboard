import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Signals & Research", "2_Signals_and_Research.py"),
        ("Focus List", "5_Focus_List.py"),
        # Order Book Rationale moved to the Execution front page (host_execution)
        # — it's an executor output, not a research artifact.
        ("Briefing Archive", "17_Research_Briefing_Archive.py"),
    ],
    key="host_research_signals",
)
