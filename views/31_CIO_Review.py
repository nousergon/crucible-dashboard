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
import plotly.graph_objects as go
import streamlit as st

from loaders.signal_loader import (  # noqa: E402
    compute_entrant_flow,
    get_available_signal_dates,
    get_entrant_detail_df,
)
from shared.constants import get_thresholds  # noqa: E402

_TH = get_thresholds()

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

eval_date = st.selectbox("Cycle (eval_date)", eval_dates, index=0, key="agent_reviews_cycle")  # shared across the Agent Reviews tabs (config#1988) — pick the cycle once

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


# ---------------------------------------------------------------------------
# Population Flow & New Entrants — moved here from Signals & Research
# (console-IA phase 2b, config#1988): these are CIO advance/reject events,
# so they belong on the CIO lens. Flow is keyed by signal date; the page's
# eval_date is resolved against the signal-date list (same Saturday cycle).
# ---------------------------------------------------------------------------
st.divider()

available_dates = get_available_signal_dates()
selected_date = None
if available_dates:
    if eval_date in available_dates:
        selected_date = eval_date
    else:
        _older = [d for d in available_dates if d <= eval_date]
        selected_date = _older[0] if _older else available_dates[0]

if selected_date is None:
    st.info("No signal dates available — population flow unavailable.")
else:
    if selected_date != eval_date:
        st.caption(
            f"No signals.json for cycle {eval_date} — showing the nearest "
            f"signal date {selected_date}."
        )
    st.subheader("Population Flow & New Entrants")
    st.caption(
        "Net-new entrant = a candidate not held the prior week that the CIO "
        "advanced (including floor-forced). A zero-add week is defensible when "
        "the fresh slate is genuinely weak; watch the trend for a saturation streak."
    )

    _pop_target = _TH.get("population_target", 25)
    _conv_bar = _TH.get("entrant_conviction_bar", 60)

    # Prior signal date (next entry in the descending list) = new-vs-held baseline.
    try:
        _sel_idx = available_dates.index(selected_date)
        _prior_date = (
            available_dates[_sel_idx + 1]
            if _sel_idx + 1 < len(available_dates)
            else None
        )
    except ValueError:
        _prior_date = None

    _flow_df = compute_entrant_flow(available_dates, weeks=12)
    _this_row = None
    if not _flow_df.empty:
        _match = _flow_df[_flow_df["date"] == selected_date]
        if not _match.empty:
            _this_row = _match.iloc[-1]

    if _this_row is None:
        st.info(
            f"No CIO decision archive (archive/agent_runs/{selected_date}/cio.json) "
            "— new-entrant stats unavailable for this date."
        )
    else:
        _nne = _this_row["net_new_entrants"]
        _nc = _this_row["new_candidates"]
        _cm = _this_row["new_conv_max"]
        _ps = _this_row["population_size"]
        fm1, fm2, fm3, fm4 = st.columns(4)
        with fm1:
            st.metric(
                "Net-new entrants",
                "—" if pd.isna(_nne) else int(_nne),
                help="Fresh names (not held last week) the CIO advanced this week.",
            )
        with fm2:
            st.metric(
                "Fresh candidates surfaced",
                "—" if pd.isna(_nc) else int(_nc),
                help="Candidates not in last week's population that the CIO evaluated.",
            )
        with fm3:
            st.metric(
                "Fresh-slate max conviction",
                "—" if pd.isna(_cm) else f"{_cm:.0f}",
                delta=None if pd.isna(_cm) else f"{_cm - _conv_bar:+.0f} vs ~{_conv_bar} bar",
                delta_color="normal",
                help=f"Highest conviction among fresh candidates. Entrants typically clear ~{_conv_bar}.",
            )
        with fm4:
            st.metric(
                "Population size",
                "—" if pd.isna(_ps) else int(_ps),
                delta=None if pd.isna(_ps) else f"{int(_ps) - _pop_target:+d} vs target {_pop_target}",
                delta_color="off",
                help=f"Held names vs target_size {_pop_target}. Above target → 0 open slots (saturation).",
            )

        if not pd.isna(_nne) and _nne == 0:
            _why = (
                f" Best fresh candidate scored {_cm:.0f} (bar ~{_conv_bar})."
                if not pd.isna(_cm)
                else ""
            )
            st.warning(
                f"**0 net-new entrants this week.**{_why} Defensible if the slate is "
                "genuinely weak — confirm via the detail table below; watch the trend "
                "for a saturation streak."
            )

    # Weekly trend: net-new entrants (bars) + fresh-slate max conviction (line).
    _disp = (
        _flow_df.dropna(subset=["net_new_entrants"])
        if not _flow_df.empty
        else _flow_df
    )
    if not _disp.empty:
        flow_fig = go.Figure()
        flow_fig.add_trace(
            go.Bar(
                x=_disp["date"],
                y=_disp["net_new_entrants"],
                name="Net-new entrants",
                marker_color="#2ca02c",
            )
        )
        flow_fig.add_trace(
            go.Scatter(
                x=_disp["date"],
                y=_disp["new_conv_max"],
                name="Fresh-slate max conviction",
                yaxis="y2",
                mode="lines+markers",
                line=dict(color="#1f77b4"),
            )
        )
        flow_fig.add_trace(
            go.Scatter(
                x=_disp["date"],
                y=[_conv_bar] * len(_disp),
                name=f"~{_conv_bar} entrant bar",
                yaxis="y2",
                mode="lines",
                line=dict(color="gray", dash="dot"),
            )
        )
        flow_fig.update_layout(
            height=320,
            margin=dict(t=30, b=0, l=0, r=0),
            yaxis=dict(title="Net-new entrants"),
            yaxis2=dict(
                title="Max conviction", overlaying="y", side="right", range=[0, 100]
            ),
            legend=dict(orientation="h", y=1.18),
        )
        st.plotly_chart(flow_fig, use_container_width=True)

    # This-week fresh-candidate detail (advanced + rejected new names).
    _detail = get_entrant_detail_df(selected_date, _prior_date)
    if not _detail.empty:
        st.markdown("**This week's fresh candidates** (not held last week)")
        st.dataframe(_detail, use_container_width=True, hide_index=True)
    elif _this_row is not None:
        st.caption(
            "No fresh candidates surfaced this week — all CIO candidates were incumbents."
        )

