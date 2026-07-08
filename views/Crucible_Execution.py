"""Crucible Results — §D Execution, the execution-sim detail (config#1957).

Evidence that fills, costs and exits are modeled honestly: per-trigger
slippage, per-exit-rule timing quality (MFE/MAE/capture), and the risk
guard's shadow-book counterfactual. All values recorded by the weekly
backtest run; the view renders via ``results.view_model``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from loaders.s3_loader import list_backtest_dates, load_backtest_file  # noqa: E402
from results import view_model as vm  # noqa: E402

st.title("Execution — fills, exits, risk guard")

dates = list_backtest_dates()
if not dates:
    st.info("No backtest runs published yet — the weekly Saturday pipeline writes `backtest/{date}/`.")
    st.stop()

date = st.selectbox("Backtest run", dates, index=0, key="crucible_exec_date")


def _json(filename: str) -> dict | None:
    loaded = load_backtest_file(date, filename)
    return loaded if isinstance(loaded, dict) else None


trigger_scorecard = _json("trigger_scorecard.json")
exit_timing = _json("exit_timing.json")
shadow_book = _json("shadow_book.json")

stats = vm.execution_headline(trigger_scorecard, exit_timing, shadow_book)
cols = st.columns(len(stats))
for col, stat in zip(cols, stats):
    col.metric(stat["label"], stat["value"], help=stat["help"])
    col.caption(stat["sub"])

st.subheader("Entry triggers", help="Execution edge of each intraday entry trigger vs the signal price and the open — the reason entries are trigger-timed instead of market-on-open.")
trig = vm.trigger_rows(trigger_scorecard)
if trig:
    st.dataframe(pd.DataFrame(trig), use_container_width=True, hide_index=True)
else:
    st.info("trigger_scorecard.json absent for this run.")

st.subheader("Exit rules", help="Per-rule timing quality: how much of the maximum favorable excursion (MFE) each exit rule captured, against the maximum adverse excursion (MAE) it tolerated.")
exits = vm.exit_type_rows(exit_timing)
if exits:
    st.dataframe(pd.DataFrame(exits), use_container_width=True, hide_index=True)
    diagnosis = (exit_timing or {}).get("diagnosis")
    if diagnosis:
        st.caption(f"Producer diagnosis: `{diagnosis}`")
else:
    st.info("exit_timing.json absent for this run.")

st.subheader("Risk guard — shadow book", help="Counterfactual replay of every candidate the risk guard blocked: did the guard's filtering add value over trading everything?")
cls = vm.shadow_classification_rows(shadow_book)
if cls:
    left, right = st.columns([1, 1])
    with left:
        st.dataframe(pd.DataFrame(cls), use_container_width=True, hide_index=True)
    with right:
        sb = shadow_book or {}
        st.markdown(
            f"Blocked **{sb.get('n_blocked', '—')}** candidates vs **{sb.get('n_traded', '—')}** traded. "
            f"Guard lift **{sb.get('guard_lift', '—')}** ({sb.get('assessment', '—')})."
        )
        st.caption(
            "The shadow book replays every candidate the risk guard blocked. A guard that only "
            "blocks losers shows positive lift; near-zero means it is currently cost-neutral."
        )
else:
    st.info("shadow_book.json absent for this run.")

with st.expander("Order blotter (all_orders.csv)"):
    orders = load_backtest_file(date, "all_orders.csv")
    if isinstance(orders, pd.DataFrame) and not orders.empty:
        st.dataframe(orders, use_container_width=True, hide_index=True)
    else:
        st.info("all_orders.csv absent for this run.")
