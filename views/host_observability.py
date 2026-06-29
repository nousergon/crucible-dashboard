import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Artifact Freshness", "26_Artifact_Freshness.py"),
        ("Active Observations", "27_Active_Observations.py"),
        ("Retros", "28_Retros.py"),
        ("Changelog", "38_Changelog.py"),
        ("Changelog Quarantine", "41_Quarantine.py"),
    ],
    key="host_observability",
)
