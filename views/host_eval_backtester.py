import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# Eval + backtester front page. The optimizer surfaces (Risk / Decision) that
# used to live here moved to the Execution front page (host_execution) — they
# are live-executor concerns, not backtest analysis.
render_host(
    [
        ("Eval Quality", "8_Eval_Quality.py"),
        ("Feedback Loop", "12_Feedback_Loop.py"),
        ("Backtester Archive", "21_Backtester_Evaluator_Archive.py"),
    ],
    key="host_eval_backtester",
)
