"""Crucible Results host page (config#1957 — the product surface, v1).

Experiment-scoped results for the Reference Rate experiment, per the plan
IA (`crucible_ux_output_plan_260708.md` §4.2): Overview / Validation /
Evaluation now; Execution and Feedback-loop tabs follow in the same arc.
Console-mounted first for dogfooding; the public crucible.nousergon.ai/dash
exposure is a routing flip gated on the trust battery (config#1958).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Overview", "Crucible_Overview.py"),
        ("Validation", "Crucible_Validation.py"),
        ("Evaluation", "Crucible_Evaluation.py"),
    ],
    key="host_crucible_results",
)
