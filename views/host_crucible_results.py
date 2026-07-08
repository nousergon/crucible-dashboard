"""Crucible Results host page (config#1957 — the product surface, v1).

Experiment-scoped results for the Reference Rate experiment, per the plan
IA (`crucible_ux_output_plan_260708.md` §4.2). The console mount carries only
the tabs that are NOT already covered by console-native pages (console-IA
phase 1, config#1990): Validation / Feedback loop / Trust. The Overview,
Evaluation and Execution views duplicated Report Card, Report Card Detail and
the Execution page's backtest sections (~80-90%) — they remain available to
the /dash skins (dash-web via dash_api, and the Streamlit rollback dash/app.py
mounts all six) through the shared `results/view_model.py` layer. The Compare
tab lands with the ablation maturation (config#1959, ≈2026-08-03).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Validation", "Crucible_Validation.py"),
        ("Feedback loop", "Crucible_Feedback.py"),
        ("Trust", "Crucible_Trust.py"),
    ],
    key="host_crucible_results",
)
