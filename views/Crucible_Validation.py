"""Crucible Results — §B Validation, the backtester detail (config#1957).

The "can you trust this backtest" surface: sub-score attribution with the
BH-FDR verdict displayed, the integrity panel (PIT parity, sample-size
adequacy, walk-forward stability, optimizer churn), and the per-signal
quality table. Absent artifacts render as explicit ABSENT rows — the
honesty is the feature (plan §8.3/§8.4).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from loaders.s3_loader import list_backtest_dates, load_backtest_file  # noqa: E402
from results import view_model as vm  # noqa: E402

st.title("Validation — backtester detail")

dates = list_backtest_dates()
if not dates:
    st.info("No backtest runs published yet — the weekly Saturday pipeline writes `backtest/{date}/`.")
    st.stop()

date = st.selectbox("Backtest run", dates, index=0, help="Weekly Saturday runs, newest first.")


def _json(filename: str) -> dict | None:
    loaded = load_backtest_file(date, filename)
    return loaded if isinstance(loaded, dict) else None


st.subheader("Integrity", help=vm.HELP["pit_parity"])
integrity = vm.integrity_rows(
    _json("pit_parity.json"),
    _json("sample_size.json"),
    _json("walk_forward_stability.json"),
    _json("optimizer_churn.json"),
)
st.dataframe(
    pd.DataFrame(integrity)[["check", "status", "detail"]],
    use_container_width=True, hide_index=True,
)

st.subheader("Attribution — sub-score → 21d outcome", help=vm.HELP["fdr"])
attr_rows = vm.attribution_rows(_json("attribution.json"))
if not attr_rows:
    st.info("attribution.json absent or empty for this run.")
else:
    df = pd.DataFrame(attr_rows)
    fig = go.Figure(go.Bar(
        x=df["correlation"], y=df["sub_score"], orientation="h",
        marker_color=["#2a78d6" if s else "#c3c2b7" for s in df["fdr_significant"]],
        hovertemplate="%{y}: %{x:.3f}<extra></extra>",
    ))
    fig.update_layout(
        height=max(220, 40 * len(df)), margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title=f"correlation vs {df['target'].iloc[0]}",
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Filled bars survive BH-FDR at q=0.05; grey bars do not. Univariate context — the primary attribution is the multivariate fit in the weekly report.")

st.subheader("Signal quality")
sq = load_backtest_file(date, "signal_quality.csv")
if isinstance(sq, pd.DataFrame) and not sq.empty:
    st.dataframe(sq, use_container_width=True, hide_index=True)
    st.caption(vm.HELP["ic"])
else:
    st.info("signal_quality.csv absent for this run.")
