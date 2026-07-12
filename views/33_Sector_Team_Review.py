"""
Sector Team Review — Alpha Engine (private console)

The per-agent view of a single sector team's decision-making for one research
cycle: the candidates it was HANDED (from the scanner→team input ledger), how
its quant analyst RANKED them, and the final 2–3 picks + the reasoning behind
them (bull/bear case, catalysts, quant rationale) — straight from the team's
run envelope.

Six teams: technology, healthcare, financials, industrials, consumer,
defensives. This is the team-level lens for diagnosing what's going wrong
inside a team's selection (companion to the CIO Review and the per-stock
Decision Review pages).

Sources: ``team_inputs`` + ``team_candidates`` in research.db, and the S3
envelope ``archive/sector_team_runs/{date}/{team_id}.json``. No LLM call.

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
    get_decision_eval_dates,
    get_team_candidates,
    get_team_inputs,
)
from loaders.s3_loader import load_sector_team_run

# Canonical sector-team ids (alpha-engine-research agents/sector_teams/team_config.py).
TEAM_IDS = ["technology", "healthcare", "financials", "industrials", "consumer", "defensives"]

st.markdown("### 🏟 Sector Team Review")
st.caption(
    "What one sector team was handed, how it ranked the candidates, and the "
    "picks + reasoning it sent up to the CIO. Read from the recorded decision "
    "trail (no LLM call, no cost)."
)

eval_dates = get_decision_eval_dates(limit=30)
if not eval_dates:
    st.warning(
        "No recorded cycles found in research.db. The decision tables populate "
        "on each Saturday research cycle."
    )
    st.stop()

col_d, col_t = st.columns([1, 1])
with col_d:
    eval_date = st.selectbox("Cycle (eval_date)", eval_dates, index=0, key="agent_reviews_cycle")  # shared across the Agent Reviews tabs (config#1988) — pick the cycle once
with col_t:
    team_id = st.selectbox("Sector team", TEAM_IDS, index=0)

inputs = get_team_inputs(eval_date, team_id)
candidates = get_team_candidates(eval_date, team_id)
envelope = load_sector_team_run(eval_date, team_id)

recs = []
if isinstance(envelope, dict):
    recs = envelope.get("recommendations") or []

# ---------------------------------------------------------------------------
# Headline funnel
# ---------------------------------------------------------------------------
n_recommended = int(candidates["team_recommended"].fillna(0).sum()) if not candidates.empty else len(recs)
m1, m2, m3 = st.columns(3)
m1.metric("Handed to team", 0 if inputs.empty else len(inputs))
m2.metric("Ranked by quant", 0 if candidates.empty else len(candidates))
m3.metric("Recommended", n_recommended)

if isinstance(envelope, dict) and (envelope.get("partial") or envelope.get("error")):
    st.warning(
        f"Team run was partial/errored this cycle — partial={envelope.get('partial')}, "
        f"error={envelope.get('error')!r}, reasons={envelope.get('partial_reasons')}. "
        "Rankings/picks below may be incomplete."
    )

st.divider()

# ---------------------------------------------------------------------------
# 1. Inputs handed to the team
# ---------------------------------------------------------------------------
st.markdown("#### 1 · Candidates handed to the team")
if not inputs.empty:
    src = inputs["source"].value_counts().to_dict()
    st.caption(
        f"From the scanner→team input ledger — {len(inputs)} names "
        f"(scanner: {src.get('scanner', 0)}, held: {src.get('held_population', 0)})."
    )
    st.dataframe(inputs, use_container_width=True, hide_index=True)
else:
    st.info(
        "No `team_inputs` ledger rows for this cycle — the complete input set is "
        "recorded only from schema v19 onward (the ledger lands on the next "
        "Saturday cycle after that producer ships). The team's ranked candidates "
        "below are the names that surfaced; the full handed-in set isn't "
        "reconstructible for pre-ledger cycles."
    )

st.divider()

# ---------------------------------------------------------------------------
# 2. Quant ranking
# ---------------------------------------------------------------------------
st.markdown("#### 2 · Quant analyst ranking")
if candidates.empty:
    st.caption("No team_candidates rows — the team didn't rank any names this cycle.")
else:
    show = candidates.copy()
    show["recommended"] = show["team_recommended"].map({1: "✅", 0: ""}).fillna("")
    cols = ["recommended", "ticker", "quant_rank", "quant_score", "qual_score",
            "rsi_sub_score", "macd_sub_score", "ma50_sub_score", "ma200_sub_score",
            "momentum_sub_score"]
    st.dataframe(show[[c for c in cols if c in show.columns]],
                 use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# 3. Final picks + reasoning
# ---------------------------------------------------------------------------
st.markdown("#### 3 · Recommended picks + reasoning")
if not recs:
    if envelope is None:
        st.info(
            f"No run envelope at archive/sector_team_runs/{eval_date}/{team_id}.json "
            "— the team's full reasoning isn't available for this cycle (envelopes "
            "are written per Saturday run)."
        )
    else:
        st.caption("The team made no BUY recommendations this cycle.")
else:
    st.caption(f"{len(recs)} pick(s). Expand for the bull/bear case, catalysts, and quant rationale.")
    for r in recs:
        if not isinstance(r, dict):
            continue
        tkr = r.get("ticker", "?")
        q, ql = r.get("quant_score"), r.get("qual_score")
        conv = r.get("conviction")
        header = f"{tkr} — quant {q}" + (f" · qual {ql}" if ql is not None else "") + \
                 (f" · conviction {conv}" if conv is not None else "")
        with st.expander(header, expanded=False):
            if r.get("quant_rationale"):
                st.markdown("**Quant rationale**"); st.write(r["quant_rationale"])
            if r.get("bull_case"):
                st.markdown("**Bull case**"); st.write(r["bull_case"])
            if r.get("bear_case"):
                st.markdown("**Bear case**"); st.write(r["bear_case"])
            cats = r.get("catalysts")
            if cats:
                st.markdown("**Catalysts**")
                if isinstance(cats, list):
                    for c in cats:
                        st.markdown(f"- {c}")
                else:
                    st.write(cats)

# Peer-review synthesis, if the envelope carries it.
if isinstance(envelope, dict):
    peer = envelope.get("peer_review_output")
    if isinstance(peer, dict) and peer:
        with st.expander("Peer-review synthesis (raw)", expanded=False):
            st.json(peer)

st.caption(
    "Want one ticker walked through the full scanner→team→CIO funnel? Use the "
    "**Decision Review** page. The committee's accept/reject lens is on **CIO Review**."
)
