import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# The System Health page was retired (console-IA phase 2a, config#1987): its
# freshness/observation strips, module-health table and missing-data alerts
# were subsumed by Fleet Status + Artifact Freshness + Active Observations;
# the surviving remainder lives on Observability's Data & Maturity tab and
# the Analysis page (Live Optimizer Params). This host keeps the agent-fleet
# surfaces. NOTE: the filename/key stay `host_system_health` — the Fleet
# Status deep-link `/host_system_health?tab=Backlog+Groom` is pinned by
# tests/test_fleet_status_page.py::TestDeepLinkTargets.
render_host(
    [
        # Pipeline Status is NOT hosted here — it owns the pinned
        # url_path="pipeline-status" slug (SF failure/complete notifications
        # deep-link to …/pipeline-status?run=<execution-name>), so it stays a
        # standalone st.Page in app.py like the other slug-owning pages
        # (director / eod-report / model-zoo / analysis). Hosting it here too
        # would move the slug onto the host and break the deep-link guard.
        ("Watch Status", "37_Watch_Status.py"),
        ("Backlog Groom", "42_Backlog_Groom.py"),
        ("Merged PRs", "47_Merged_PRs.py"),
        # config#646 — the fleet's flow-doctor end-of-run heartbeat ("alive but
        # quiet" vs "suppressing X per flow"), the System Health consumer for the
        # "make it actually kick in" arc.
        ("Flow-Doctor Heartbeat", "27_Flow_Doctor_Heartbeat.py"),
    ],
    key="host_system_health",
)
