import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# "Predictor" (7_Predictor.py) was pulled out to a standalone pinned page
# (config#856 — url_path="predictor", the predictor's slim morning-briefing
# email deep-link target) — see app.py. It no longer lives in this tab list
# to avoid double-registration.
render_host(
    [
        ("Regime", "15_Regime.py"),
        ("Feature Store", "13_Feature_Store.py"),
        ("Briefing Archive", "18_Predictor_Briefing_Archive.py"),
        ("Training Archive", "20_Predictor_Training_Archive.py"),
    ],
    key="host_predictor",
)
