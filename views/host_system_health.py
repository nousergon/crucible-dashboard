import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("System Health", "4_System_Health.py"),
        ("Pipeline Status", "25_Pipeline_Status.py"),
        ("Saturday SF Watch", "37_Saturday_SF_Watch.py"),
    ],
    key="host_system_health",
)
