"""Crucible Results — §A Overview (config#1957, plan §4.2).

Experiment-scoped front page for the Reference Rate experiment: identity
block (what ran, exactly) → headline stat strip → Report Card v2 tiles →
equity curve vs SPY. Reads versioned artifacts only; renders via the
skin-agnostic ``results.view_model`` so the future public /dash skin
consumes the identical layer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from components.report_card_v2 import render_overview  # noqa: E402
from loaders.s3_loader import (  # noqa: E402
    list_backtest_dates,
    load_eod_pnl,
    load_report_card,
)
from results import view_model as vm  # noqa: E402

st.title("⚗ Crucible — Reference Rate")
st.caption(
    "The stock reference experiment, graded by the harness that ran it. "
    "Paper-traded, illustrative only — not investment advice."
)

card = load_report_card()
dates = list_backtest_dates()
backtest_date = dates[0] if dates else None
identity = vm.build_identity(card, backtest_date)

with st.container(border=True):
    left, right = st.columns([2, 3])
    with left:
        st.markdown(f"**Experiment** · `{identity['experiment_id']}`")
        st.caption(
            f"report card {identity['report_card_date']} · "
            f"backtest {identity['backtest_date']} · "
            f"grader {identity['grader_source']}"
        )
    with right:
        for slot, impl in identity["slots"]:
            st.markdown(f"`{slot}` — {impl}")

eod = load_eod_pnl()
signal_metrics = portfolio_stats = None
if backtest_date:
    from loaders.s3_loader import load_backtest_file
    loaded = load_backtest_file(backtest_date, "metrics.json")
    signal_metrics = loaded if isinstance(loaded, dict) else None
    loaded = load_backtest_file(backtest_date, "portfolio_stats.json")
    portfolio_stats = loaded if isinstance(loaded, dict) else None

stats = vm.build_headline(eod, signal_metrics, portfolio_stats)
cols = st.columns(len(stats))
for col, stat in zip(cols, stats):
    col.metric(stat["label"], stat["value"], help=stat["help"])
    col.caption(stat["sub"])

st.subheader("Report Card")
render_overview(card)

st.subheader("Cumulative return vs SPY")
eq = vm.equity_frame(eod)
if eq.empty:
    st.info("No EOD P&L history available yet — the curve renders once `trades/eod_pnl.csv` has rows.")
else:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["date"], y=eq["SPY"], name="SPY",
        line=dict(color="#848d98", width=2), hovertemplate="%{x} · SPY %{y:.2f}%<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=eq["date"], y=eq["Portfolio"], name="Portfolio",
        line=dict(color="#2a78d6", width=2), hovertemplate="%{x} · Portfolio %{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title="Cumulative return (%)", hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)
st.caption(vm.HELP["alpha"])
