import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# Eval + backtester front page. The optimizer surfaces (Risk / Decision)
# moved to the Execution front page; the Backtester Archive tab was retired
# in phase 1 (Analysis renders report.md for all runs); Feedback Loop was
# absorbed into Analysis' Self-Tuning tab in phase 2b (config#1988). The
# host survives with one tab because the pipeline-status registry deep-link
# `host_eval_backtester?tab=Eval+Quality` (nousergon-lib v0.96.0) points
# here — guarded by tests/test_registry_page_targets.py.
render_host(
    [
        ("Eval Quality", "8_Eval_Quality.py"),
    ],
    key="host_eval_backtester",
)
