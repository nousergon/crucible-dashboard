"""
Experiments — Alpha Engine (private console)

The champion/challenger ABLATION experiments (ARCHITECTURE §37): observe-only
substrates that run the challenger(s) beside the live champion every weekly
cycle and score both on realized 21-trading-day forward returns. Nothing here
is read by live trading — this page is the evidence ledger for promotion
decisions.

Experiments rendered (one tab each; more join as substrates ship):
- Producer ablation (config#1223 / #1403): the live agentic LangGraph research
  producer vs ``no_agent_quant`` (deterministic quant floor, no LLM) and
  ``single_agent_quant`` (one Sonnet call) — "does the agentic layer earn its
  keep?" Cohorts: ``signals_shadow/{producer}/``; leaderboard:
  ``research/producer_leaderboard/``.
- Scanner ablation (config#1221): the live scanner vs the ``momentum_sleeve``
  challenger. Cohorts: ``candidates_shadow/{spec}/``; leaderboard:
  ``scanner/leaderboard/``.

Honest empty state: a cohort scores only after its 21-trading-day horizon
matures, so a young experiment shows emitted-but-unmatured cohorts, not
metrics. Reads only recorded S3 artifacts — no LLM call, no cost. Native
Streamlit chrome — no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    list_leaderboard_dates,
    list_shadow_cohort_dates,
    load_leaderboard,
)

_HORIZON_TRADING_DAYS = 21  # scoring/leaderboard_producers.DEFAULT_HORIZON_DAYS

_PRODUCER_LB_PREFIX = "research/producer_leaderboard/"
_SCANNER_LB_PREFIX = "scanner/leaderboard/"
_PRODUCER_COHORT_PREFIXES = {
    "no_agent_quant": "signals_shadow/no_agent_quant/",
    "single_agent_quant": "signals_shadow/single_agent_quant/",
}
_SCANNER_COHORT_PREFIXES = {
    "momentum_sleeve": "candidates_shadow/momentum_sleeve/",
}

_METRIC_COLUMNS = {
    "name": "Spec",
    "kind": "Kind",
    "realized_rank_ic": "Realized rank-IC (21d)",
    "topn_alpha_vs_champion": "Top-N alpha vs champion",
    "n_dates_scored": "Cohorts scored",
}


def _maturity_date(cohort: str) -> date | None:
    """Approximate maturation date: cohort + 21 business days (NYSE holidays
    ignored — the leaderboard's own trading-day join is authoritative; this is
    display guidance only, hence the ≈ in the UI)."""
    try:
        start = datetime.strptime(cohort, "%Y-%m-%d").date()
    except ValueError:
        return None
    return pd.bdate_range(start=start, periods=_HORIZON_TRADING_DAYS + 1)[-1].date()


def _cohort_frame(cohort_prefixes: dict[str, str]) -> pd.DataFrame:
    rows = []
    today = date.today()
    for spec, prefix in cohort_prefixes.items():
        for cohort in list_shadow_cohort_dates(prefix):
            matures = _maturity_date(cohort)
            rows.append({
                "Spec": spec,
                "Cohort date": cohort,
                "Matures ≈": str(matures) if matures else "?",
                "Status": "matured" if matures and matures <= today else "maturing",
            })
    return pd.DataFrame(rows)


def _spec_frame(lb: dict) -> pd.DataFrame:
    df = pd.DataFrame(lb.get("specs", []))
    if df.empty:
        return df
    cols = [c for c in _METRIC_COLUMNS if c in df.columns]
    return df[cols].rename(columns=_METRIC_COLUMNS)


def _history_frame(lb_prefix: str, dates: list[str], limit: int = 30) -> pd.DataFrame:
    rows = []
    for d in dates[-limit:]:
        lb = load_leaderboard(lb_prefix, d)
        if not isinstance(lb, dict):
            continue
        for spec in lb.get("specs", []):
            if spec.get("realized_rank_ic") is not None:
                rows.append({
                    "build_date": d,
                    "spec": spec.get("name"),
                    "realized_rank_ic": spec.get("realized_rank_ic"),
                })
    return pd.DataFrame(rows)


def _render_experiment(
    *, title: str, blurb: str, lb_prefix: str, cohort_prefixes: dict[str, str],
) -> None:
    st.subheader(title)
    st.caption(blurb)

    lb_dates = list_leaderboard_dates(lb_prefix)
    cohorts = _cohort_frame(cohort_prefixes)
    n_matured = int((cohorts["Status"] == "matured").sum()) if not cohorts.empty else 0

    c1, c2, c3 = st.columns(3)
    c1.metric("Cohorts emitted", 0 if cohorts.empty else len(cohorts))
    c2.metric("Cohorts matured (≈)", n_matured)
    c3.metric("Leaderboard builds", len(lb_dates))

    if not lb_dates:
        st.info("No leaderboard builds yet — the weekly scorer has not run for this experiment.")
    else:
        pick = st.selectbox(
            "Leaderboard build", list(reversed(lb_dates)), key=f"lb_{lb_prefix}",
        )
        lb = load_leaderboard(lb_prefix, pick)
        if not isinstance(lb, dict):
            st.warning(f"Leaderboard {pick} failed to load.")
        else:
            scored = int(lb.get("n_dates") or 0)
            if scored == 0:
                first = cohorts["Matures ≈"].min() if not cohorts.empty else None
                st.info(
                    "No matured cohorts scored yet — metrics are an honest "
                    "``None`` until a cohort's "
                    f"{_HORIZON_TRADING_DAYS}-trading-day horizon closes"
                    + (f" (first ≈ {first})." if first else ".")
                )
            df = _spec_frame(lb)
            if not df.empty:
                st.dataframe(df, use_container_width=True, hide_index=True)

        hist = _history_frame(lb_prefix, lb_dates)
        if not hist.empty:
            st.line_chart(
                hist.pivot(index="build_date", columns="spec", values="realized_rank_ic"),
            )

    with st.expander("Cohorts (challenger shadow emissions)"):
        if cohorts.empty:
            st.write("No cohorts emitted yet.")
        else:
            st.dataframe(cohorts, use_container_width=True, hide_index=True)


st.title("⚗️ Experiments")
st.caption(
    "Champion/challenger observe substrates (ARCHITECTURE §37) — challengers "
    "run beside the live champion each weekly cycle; both are scored on "
    "realized 21-trading-day forward returns. Observe-only: never read by "
    "live trading. Promotion is manual and evidence-gated."
)

producer_tab, scanner_tab = st.tabs(["Producer ablation", "Scanner ablation"])

with producer_tab:
    _render_experiment(
        title="Agentic research producer vs quant baselines",
        blurb=(
            "config#1223 / #1403 — the live multi-agent LangGraph producer "
            "(champion) vs a deterministic no-LLM quant floor and a single-"
            "Sonnet-call producer, all selecting from the same scanner "
            "candidates and prior population. The question: does the agentic "
            "layer's marginal selection earn alpha over its cost?"
        ),
        lb_prefix=_PRODUCER_LB_PREFIX,
        cohort_prefixes=_PRODUCER_COHORT_PREFIXES,
    )

with scanner_tab:
    _render_experiment(
        title="Scanner champion vs momentum sleeve",
        blurb=(
            "config#1221 — the live scanner (champion candidate feed) vs the "
            "momentum_sleeve challenger, scored on the scanner's own long-only "
            "top-N objective. The attractiveness-feed counterfactual "
            "(scanner_factor_counterfactual) is scored separately in the weekly "
            "e2e_lift artifact."
        ),
        lb_prefix=_SCANNER_LB_PREFIX,
        cohort_prefixes=_SCANNER_COHORT_PREFIXES,
    )
