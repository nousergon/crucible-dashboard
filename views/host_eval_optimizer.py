import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Eval Quality", "8_Eval_Quality.py"),
        ("Feedback Loop", "12_Feedback_Loop.py"),
        ("Optimizer Risk", "30_Optimizer_Risk.py"),
        ("Optimizer Decision", "32_Optimizer_Decision.py"),
        ("Backtester Archive", "21_Backtester_Evaluator_Archive.py"),
    ],
    key="host_eval_optimizer",
)
