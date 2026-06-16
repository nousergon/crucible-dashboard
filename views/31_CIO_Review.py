"""
CIO Review — Alpha Engine (private console)

The per-agent view of the **CIO batch evaluation** stage: for one research
cycle, what was handed TO the CIO (the union of every sector team's
recommendations), and what the CIO did with each name — ADVANCE / REJECT /
ADVANCE_FORCED / NO_ADVANCE_DEADLOCK — with its conviction, rank, blended
scores, and free-form rationale.

Complements the per-stock ``29_Decision_Review.py`` (walks one ticker through
the whole funnel) and the per-team sector pages: this one is the CIO lens, for
understanding what's going wrong in the committee's selection. Read straight
from ``cio_evaluations`` + ``team_candidates`` in research.db — no LLM call, no
cost.

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome — no set_page_config (the st.navigation entrypoint in app.py owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.db_loader import (
    _ADVANCE_DECISIONS,
    get_cio_evaluations,
    get_cio_inputs,
    get_cycle_funnel,
    get_decision_eval_dates,
)

st.markdown("### 🏛 CIO Review")
st.caption(
    "What the Investment Committee CIO was handed — and what it chose. "
    "Inputs are the union of every sector team's recommendations; the CIO "
    "gates entrants (ADVANCE / REJECT / ADVANCE_FORCED / deadlock). Read "
    "straight from the recorded decision trail (no LLM call, no cost)."
)

eval_dates = get_decision_eval_dates(limit=30)
if not eval_dates:
    st.warning(
        "No recorded cycles found in research.db (cio_evaluations / "
        "team_candidates). The decision tables populate on each Saturday "
        "research cycle."
    )
    st.stop()

eval_date = st.selectbox("Cycle (eval_date)", eval_dates, index=0)

funnel = get_cycle_funnel(eval_date)
inputs = get_cio_inputs(eval_date)
evals = get_cio_evaluations(eval_date)

# ---------------------------------------------------------------------------
# Cycle headline
# ---------------------------------------------------------------------------
m1, m2, m3 = st.columns(3)
m1.metric("Handed to CIO (team recs)",
          0 if inputs.empty else len(inputs))
m2.metric("CIO evaluated", funnel["cio_evaluated"])
m3.metric("Advanced", funnel["cio_advanced"])

if evals.empty:
    st.info(
        f"No cio_evaluations rows for {eval_date} — the CIO stage may not have "
        "run or persisted that cycle. The input set below is what it would have "
        "received."
    )

st.divider()

# ---------------------------------------------------------------------------
# 1. Inputs — what the sector teams handed up
# ---------------------------------------------------------------------------
st.markdown("#### 1 · Handed to the CIO")
if inputs.empty:
    st.caption(
        "No team_recommended=1 rows in team_candidates for this cycle — no "
        "sector team surfaced a pick (or the table wasn't populated)."
    )
else:
    by_team = (
        inputs.groupby("team_id")["ticker"]
        .agg(["count", lambda s: ", ".join(sorted(s.astype(str)))])
        .rename(columns={"count": "n_recs", "<lambda_0>": "tickers"})
        .reset_index()
        .sort_values("team_id")
    )
    st.dataframe(by_team, use_container_width=True, hide_index=True)
    with st.expander(f"Per-ticker input scores ({len(inputs)})", expanded=False):
        st.dataframe(inputs, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# 2. CIO decisions — advanced vs not
# ---------------------------------------------------------------------------
st.markdown("#### 2 · CIO decisions")
if not evals.empty:
    dec = evals["cio_decision"].astype(str).str.upper()
    advanced = evals[dec.isin(_ADVANCE_DECISIONS)]
    rejected = evals[~dec.isin(_ADVANCE_DECISIONS)]

    _cols = [
        "ticker", "team_id", "cio_decision", "cio_rank", "cio_conviction",
        "final_score", "combined_score", "quant_score", "qual_score",
        "macro_shift",
    ]

    def _present(df: pd.DataFrame) -> pd.DataFrame:
        return df[[c for c in _cols if c in df.columns]]

    st.markdown(f"**✅ Advanced ({len(advanced)})**")
    if advanced.empty:
        st.caption("None advanced this cycle.")
    else:
        st.dataframe(_present(advanced), use_container_width=True, hide_index=True)

    st.markdown(f"**🛑 Not advanced ({len(rejected)})**")
    if rejected.empty:
        st.caption("Every evaluated name advanced.")
    else:
        st.dataframe(_present(rejected), use_container_width=True, hide_index=True)

    st.divider()

    # -----------------------------------------------------------------------
    # 3. Per-ticker rationale drilldown
    # -----------------------------------------------------------------------
    st.markdown("#### 3 · Rationale")
    tickers = evals["ticker"].astype(str).tolist()
    sel = st.selectbox("Ticker", tickers, index=0)
    row = evals[evals["ticker"].astype(str) == sel].iloc[0]
    decision = str(row.get("cio_decision") or "").upper()
    chosen = decision in _ADVANCE_DECISIONS
    head = (
        f"{'✅' if chosen else '🛑'} **{sel}** — {decision or '(no decision)'} · "
        f"rank {row.get('cio_rank')} · conviction {row.get('cio_conviction')} · "
        f"final_score {row.get('final_score')}"
    )
    (st.success if chosen else st.warning)(head)
    st.markdown("**Rationale**")
    st.write(row.get("rationale") or "_(none recorded)_")
    tags = row.get("rule_tags")
    if tags and str(tags).strip() not in ("", "[]", "null", "None"):
        st.markdown("**Rule tags**")
        st.code(str(tags), language="json")

st.caption(
    "Want one ticker walked through the full scanner→team→CIO funnel, or the "
    "agent's free-form reasoning? Use the **Decision Review** page, or the CLI "
    "fallback `python -m scripts.decision_review ask <TICKER> \"<question>\"` "
    "in alpha-engine-research."
)
