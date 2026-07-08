"""Crucible Results — §C Evaluation, the evaluator detail (config#1957).

The full MetricRecord contract rendered per tile, not summarized: value,
CI, N, target/red-line, trend, criticality, status and — load-bearing for
the trust story — the operator-readable ``status_reason`` on every row.
Renders via ``results.view_model`` so the public /dash skin reuses the
identical layer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from loaders.s3_loader import load_report_card  # noqa: E402
from results import view_model as vm  # noqa: E402

st.title("Evaluation — evaluator detail")
st.caption(
    "Every metric carries its confidence interval, sample size, target, "
    "red line and the reason for its status. An N/A grade always states why."
)

card = load_report_card()
if not card:
    st.info("No Report Card published yet — the evaluator writes `evaluator/{date}/report_card.json` each Saturday.")
    st.stop()

labels = [(k, lbl) for k, lbl in vm.tile_labels(card) if k in vm.EXPERIMENT_TILES or k == "portfolio_outcome"]
if not labels:
    st.warning("Report card carries no experiment tiles — unexpected; check the grading Lambda output.")
    st.stop()

label_by_key = dict(labels)
tile_key = st.selectbox(
    "Tile", [k for k, _ in labels], format_func=lambda k: label_by_key[k],
    help="Grader verdicts on this experiment's components. System-operations tiles (substrate, agent infrastructure, grader self-checks) are internal and live on the operator console.",
)

rows = vm.metric_rows(card, tile_key)
if not rows:
    st.info("This tile has no MetricRecord components on the current card.")
else:
    st.dataframe(
        pd.DataFrame(rows)[
            ["metric", "criticality", "status", "value", "ci", "n",
             "target", "red_line", "trend", "reason"]
        ],
        use_container_width=True, hide_index=True,
        column_config={"reason": st.column_config.TextColumn("why", width="large")},
    )
    st.caption(vm.HELP["ic"] + " " + vm.HELP["dsr"])
