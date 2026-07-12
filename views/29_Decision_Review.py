"""
Decision Review — Alpha Engine (private console)

Answers "what did the system decide about ticker X, and why was it (or wasn't
it) chosen?" straight from the decision audit trail the research pipeline
writes every cycle (scanner_evaluations / team_candidates / cio_evaluations /
investment_thesis in research.db). No LLM call — this is the artifact-first
review surface (ROADMAP L4567 Phase 3); the CLI's free-form `ask` fallback
(Phase 2) stays in alpha-engine-research for now.

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome per [[reference_dashboard_chrome_dichotomy]] — no set_page_config (the
st.navigation entrypoint in app.py owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.db_loader import (
    explain_why_not,
    get_cycle_funnel,
    get_decision_eval_dates,
    get_ticker_decision,
)

st.markdown("### 🔍 Decision Review")
st.caption(
    "Why the research pipeline did — or didn't — pick a stock, read straight "
    "from the recorded decision trail (no LLM call, no cost). Funnel: "
    "**scanner gate → sector-team rank → CIO decision**."
)

eval_dates = get_decision_eval_dates(limit=30)
if not eval_dates:
    st.warning(
        "No recorded decisions found in research.db "
        "(scanner_evaluations / team_candidates / cio_evaluations). "
        "The decision tables populate on each Saturday research cycle."
    )
    st.stop()

col_date, col_ticker = st.columns([1, 1])
with col_date:
    eval_date = st.selectbox("Cycle (eval_date)", eval_dates, index=0, key="agent_reviews_cycle")  # shared across the Agent Reviews tabs (config#1988) — pick the cycle once
with col_ticker:
    ticker = st.text_input("Ticker", value="", placeholder="e.g. NVDA").strip().upper()

# ---------------------------------------------------------------------------
# Cycle funnel summary
# ---------------------------------------------------------------------------
funnel = get_cycle_funnel(eval_date)
st.markdown(f"#### Cycle {eval_date} — funnel")
m1, m2, m3 = st.columns(3)
m1.metric("Scanner: passed / screened",
          f"{funnel['scanner_passed']} / {funnel['scanner_screened']}")
m2.metric("Teams: recommended / ranked",
          f"{funnel['team_recommended']} / {funnel['team_ranked']}")
m3.metric("CIO: advanced / evaluated",
          f"{funnel['cio_advanced']} / {funnel['cio_evaluated']}")

advanced = funnel.get("advanced")
if isinstance(advanced, pd.DataFrame) and not advanced.empty:
    with st.expander(f"Advanced this cycle ({len(advanced)})", expanded=False):
        st.dataframe(advanced, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Per-ticker review
# ---------------------------------------------------------------------------
if not ticker:
    st.info("Enter a ticker above to see its full decision record + why-not verdict.")
    st.stop()

verdict = explain_why_not(ticker, eval_date)
stage = verdict["stage"]
_icon = {"chosen": "✅", "scanner": "🚫", "team": "📉", "cio": "🛑",
         "no_record": "❔"}.get(stage, "•")
st.markdown(f"#### {_icon} {ticker} — {stage.replace('_', ' ').upper()}")
(st.success if stage == "chosen" else st.info if stage == "no_record" else st.warning)(
    verdict["verdict"]
)

record = get_ticker_decision(ticker, eval_date)


def _show(title: str, df: pd.DataFrame, empty_msg: str) -> None:
    with st.expander(title, expanded=True):
        if df is None or df.empty:
            st.caption(empty_msg)
        else:
            # Transpose single-row frames to a readable field/value view.
            if len(df) == 1:
                s = df.iloc[0].dropna()
                st.dataframe(
                    pd.DataFrame({"field": s.index, "value": s.values}),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.dataframe(df, use_container_width=True, hide_index=True)


_show("Scanner (quant filter)", record["scanner"],
      "No scanner_evaluations row — ticker not in the screened universe this cycle.")
_show("Sector team(s) — quant rank", record["team_candidates"],
      "No team_candidates row — not surfaced in any team's ranking.")
_show("CIO decision", record["cio"],
      "No cio_evaluations row — did not reach the CIO.")
_show("Investment thesis", record["thesis"],
      "No investment_thesis row for this date.")

st.caption(
    "Need the agent's free-form reasoning or a 'what-if'? Use the CLI fallback: "
    "`python -m scripts.decision_review ask <TICKER> \"<question>\"` "
    "in alpha-engine-research (grounds an LLM in the captured artifacts)."
)
