import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Artifact Freshness", "26_Artifact_Freshness.py"),
        ("Active Observations", "27_Active_Observations.py"),
        # Changelog + Retros + Quarantine consolidated into one Incidents tab
        # (three lenses on the same changelog corpus) — console-IA phase 1,
        # config#1990. Sub-lens selection lives inside views/Incidents.py.
        ("Incidents", "Incidents.py"),
    ],
    key="host_observability",
)
