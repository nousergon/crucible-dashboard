"""Crucible Results — §A Overview (config#1957; prosumer reframe plan §9.2/9-A).

Tear-sheet-first front page for the Reference Rate experiment: identity
block → performance headline (numbers with CIs, never letter grades) →
measurement-integrity strip (the green-able honesty layer) → grader
verdicts scoped to the EXPERIMENT tiles → equity curve vs SPY. Ops tiles
(substrate/agent/backtester-self/director_quality) render on the console
Report Card only — an outside strategy-tester never sees our plumbing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from loaders.s3_loader import (  # noqa: E402
    list_backtest_dates,
    load_backtest_file,
    load_eod_pnl,
    load_report_card,
)
from loaders.trust_battery_loader import load_ci_verdicts  # noqa: E402
from results import view_model as vm  # noqa: E402
from results.battery_registry import BATTERY_LEGS  # noqa: E402

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

def _bt_json(filename: str) -> dict | None:
    if not backtest_date:
        return None
    loaded = load_backtest_file(backtest_date, filename)
    return loaded if isinstance(loaded, dict) else None


eod = load_eod_pnl()
signal_metrics = _bt_json("metrics.json")
portfolio_stats = _bt_json("portfolio_stats.json")

stats = vm.build_headline(eod, signal_metrics, portfolio_stats)
cols = st.columns(len(stats))
for col, stat in zip(cols, stats):
    col.metric(stat["label"], stat["value"], help=stat["help"])
    col.caption(stat["sub"])

st.subheader("Measurement integrity")
st.caption(
    "Whether these numbers can be trusted — the part that must always be green. "
    "Full detail on the Trust and Validation tabs."
)
_repos = tuple(sorted({leg["repo"] for leg in BATTERY_LEGS}))
_ci = load_ci_verdicts(_repos)
_battery_ok = all(v.get("conclusion") == "success" for v in _ci.values()) if _ci else False
integrity = vm.integrity_rows(
    _bt_json("pit_parity.json"),
    _bt_json("sample_size.json"),
    _bt_json("walk_forward_stability.json"),
    _bt_json("optimizer_churn.json"),
)
_lookahead = next((r for r in integrity if "Lookahead" in r["check"]), None)
_sample = next((r for r in integrity if "Sample" in r["check"]), None)
i1, i2, i3 = st.columns(3)
i1.metric(
    "Validation battery",
    "passing" if _battery_ok else "check Trust tab",
    help="Named engine + grader validation suites, vouched for by each repo's live main-branch CI — see the Trust tab.",
)
i2.metric(
    "Lookahead audit",
    (_lookahead["detail"].split(": ")[-1] if _lookahead and _lookahead["status"] != "ABSENT" else "—"),
    help=vm.HELP["pit_parity"],
)
i3.metric(
    "Sample adequacy",
    (_sample["status"] if _sample else "—"),
    help="Finalized-signal count vs the minimum-N floor on the weakest measurement leg.",
)

st.subheader("Grader verdicts — this strategy")
st.caption(
    "What the evaluation layer concludes about the experiment's components, reasons included. "
    "Honest negatives stay visible — performance itself is reported as numbers above, never as a grade."
)
verdicts = vm.experiment_tile_verdicts(card)
if not verdicts:
    st.info("No graded card published yet.")
else:
    chip = {"GREEN": "🟢", "WATCH": "🟡", "RED": "🔴"}
    vcols = st.columns(len(verdicts))
    for col, row in zip(vcols, verdicts):
        icon = chip.get(row["status"], "⚪")
        with col:
            with st.container(border=True):
                st.markdown(f"**{icon} {row['tile'].replace('_', ' ').title()}**")
                st.caption(f"{row['graded']}/{row['total']} components graded")
                st.caption(row["reason"][:140] or "—")

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

st.subheader("Alpha over time")
period = st.segmented_control(
    "Period", ["Daily", "Weekly", "Monthly"], default="Weekly",
    key="crucible_alpha_period",
    help="Ledger daily alpha aggregated per period since inception — the dissection view for 'has it been improving?'",
)
period_code = {"Daily": "D", "Weekly": "W", "Monthly": "M"}[period or "Weekly"]
alpha_df = vm.alpha_by_period(eod, period_code)
if alpha_df.empty:
    st.info("No daily-alpha history in the EOD ledger yet.")
else:
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=alpha_df["label"], y=alpha_df["alpha_pct"],
        name=f"{period} alpha",
        marker_color=["#2ca02c" if v >= 0 else "#d62728" for v in alpha_df["alpha_pct"]],
        customdata=alpha_df["n_days"],
        hovertemplate="%{x} · %{y:+.2f}% (%{customdata}d)<extra></extra>",
    ))
    if period_code == "D":
        roll = vm.rolling_alpha_frame(eod)
        if not roll.empty:
            fig.add_trace(go.Scatter(
                x=roll["date"], y=roll["rolling_mean"], name="20-session rolling mean",
                line=dict(color="#2a78d6", width=2),
                hovertemplate="%{x} · 20d mean %{y:+.3f}%<extra></extra>",
            ))
    fig.add_hline(y=0, line=dict(color="rgba(128,128,128,0.5)", width=1, dash="dot"))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title=f"{period} alpha (%)", hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        showlegend=(period_code == "D"),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Per-period sums of the recorded daily alpha ledger (matches the headline's cumulative "
        "convention). The rolling mean is descriptive — statistical trend adjudication (slope, "
        "significance) is the evaluator's job, not this chart's."
    )
