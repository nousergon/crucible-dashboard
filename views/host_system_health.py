import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("System Health", "4_System_Health.py"),
        # Pipeline Status is NOT hosted here — it owns the pinned
        # url_path="pipeline-status" slug (SF failure/complete notifications
        # deep-link to …/pipeline-status?run=<execution-name>), so it stays a
        # standalone st.Page in app.py like the other slug-owning pages
        # (director / eod-report / model-zoo / analysis). Hosting it here too
        # would move the slug onto the host and break the deep-link guard.
        ("Saturday SF Watch", "37_Saturday_SF_Watch.py"),
        ("Backlog Groom", "42_Backlog_Groom.py"),
        ("Merged PRs", "47_Merged_PRs.py"),
    ],
    key="host_system_health",
)
