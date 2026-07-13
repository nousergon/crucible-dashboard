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
- Champion loop (config#2364/#2367/#2369): NOT observe-only like the two
  tabs above — this is the GATED executor selection-path switch (agentic vs
  scanner_predictor_direct). Shows the live pointer, weekly promotion/
  demotion audit history, per-arm gate state (eligible / hysteresis /
  cooldown / insufficient-data), and the challenger's weekly sector-neutral
  lift series. Read-only console surface; the pointer itself is written by
  crucible-backtester's weekly gate engine or a one-shot operator bootstrap.

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
    list_champion_audit_dates,
    list_champion_leaderboard_dates,
    list_leaderboard_dates,
    list_shadow_cohort_dates,
    load_champion_audit,
    load_champion_audit_latest,
    load_champion_leaderboard,
    load_champion_pointer,
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

_CHAMPION_ARMS = ("agentic", "scanner_predictor_direct")
_CHAMPION_ARM_LABELS = {
    "agentic": "Agentic (full research pipeline)",
    "scanner_predictor_direct": "Scanner → predictor (no agent)",
}
_BLOCKED_BY_LABELS = {
    "insufficient_matured_cohorts": "insufficient data",
    "cooldown_active": "cooldown",
    "not_significant_hac_adjusted": "not significant (HAC-adjusted)",
    "hysteresis_not_satisfied": "hysteresis pending",
    "frozen": "frozen (--freeze)",
    "leaderboard_unavailable": "leaderboard unavailable",
    "unclassified_error": "error",
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


def _gate_state_label(audit: dict) -> str:
    """Human label for one weekly audit record's outcome, per contracts/
    producer_champion_audit.schema.json's ``outcome``/``blocked_by`` enums."""
    outcome = audit.get("outcome")
    if outcome in ("promoted", "demoted"):
        return f"{outcome} this run"
    if outcome == "error":
        return f"error: {audit.get('detail', 'unclassified')}"
    blocked = audit.get("blocked_by") or []
    if not blocked:
        return outcome or "unknown"
    wins = audit.get("consecutive_wins")
    labels = []
    for b in blocked:
        lbl = _BLOCKED_BY_LABELS.get(b, b)
        if b == "hysteresis_not_satisfied" and wins is not None:
            lbl = f"{lbl} ({wins}/2)"
        labels.append(lbl)
    return ", ".join(labels)


def _champion_history_frame(dates: list[str], limit: int = 30) -> pd.DataFrame:
    rows = []
    for d in dates[-limit:]:
        audit = load_champion_audit(d)
        if not isinstance(audit, dict):
            continue
        rows.append({
            "Date": audit.get("date", d),
            "Outcome": audit.get("outcome"),
            "Champion before": audit.get("champion_before"),
            "Champion after": audit.get("champion_after"),
            "Source": audit.get("promotion_source", "gate_engine"),
            "Matured cohorts": audit.get("challenger_matured_cohorts"),
            "SN lift vs champion": audit.get("sn_lift_vs_champion"),
            "Consecutive wins": audit.get("consecutive_wins"),
            "Cooldown until": audit.get("cooldown_until"),
            "Gate state": _gate_state_label(audit),
        })
    return pd.DataFrame(rows)


def _champion_leaderboard_history_frame(dates: list[str], limit: int = 30) -> pd.DataFrame:
    rows = []
    for d in dates[-limit:]:
        lb = load_champion_leaderboard(d)
        if not isinstance(lb, dict):
            continue
        for point in lb.get("weekly_points", []):
            if point.get("sn_lift_vs_agentic_cio") is not None:
                rows.append({
                    "build_date": point.get("date", d),
                    "sn_lift_vs_agentic_cio": point.get("sn_lift_vs_agentic_cio"),
                    "n_picks": point.get("n_picks"),
                    "n_cycles": point.get("n_cycles"),
                })
    return pd.DataFrame(rows)


def _render_champion_loop() -> None:
    st.subheader("Champion/challenger promotion loop")
    st.caption(
        "config#2364 / #2367 — NOT observe-only like the ablation tabs above: "
        "this is the GATED executor selection-path switch (agentic vs "
        "scanner_predictor_direct). A pointer move here changes what the "
        "live executor trades starting the next daily preopen run."
    )

    pointer = load_champion_pointer()
    current_champion = (pointer or {}).get("champion", "agentic")

    if pointer is None:
        st.info(
            "No champion pointer written yet — the executor defaults to "
            "'agentic' (pre-bootstrap)."
        )
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric(
            "Current champion",
            _CHAMPION_ARM_LABELS.get(current_champion, current_champion),
        )
        c2.metric("Promotion source", pointer.get("promotion_source", "?"))
        c3.metric(
            "Promoted at",
            str(pointer.get("promoted_at", "?"))[:19].replace("T", " "),
        )

    audit_dates = list_champion_audit_dates()
    if not audit_dates:
        st.info(
            "No weekly audit records yet — the gate engine has not run "
            "since this loop shipped (config#2367, 2026-07-13)."
        )
    else:
        latest = load_champion_audit_latest()
        if isinstance(latest, dict):
            challenger = latest.get("challenger") or next(
                (a for a in _CHAMPION_ARMS if a != current_champion), None,
            )
            g1, g2 = st.columns(2)
            g1.metric(
                f"{_CHAMPION_ARM_LABELS.get(current_champion, current_champion)} (champion)",
                "live",
            )
            if challenger:
                g2.metric(
                    f"{_CHAMPION_ARM_LABELS.get(challenger, challenger)} (challenger)",
                    _gate_state_label(latest),
                )

        st.markdown("**Promotion history**")
        hist = _champion_history_frame(audit_dates)
        if not hist.empty:
            st.dataframe(hist, use_container_width=True, hide_index=True)

    st.markdown("**Weekly sector-neutral lift (challenger vs champion)**")
    lb_dates = list_champion_leaderboard_dates()
    if not lb_dates:
        st.info(
            "No champion-gate leaderboard builds yet — honest absence "
            "until the e2e_lift counterfactual matures its first cohort."
        )
    else:
        lb_hist = _champion_leaderboard_history_frame(lb_dates)
        if lb_hist.empty:
            st.info(
                "Leaderboard builds exist but no cohort has matured yet "
                "(honest None until the 21-trading-day horizon closes)."
            )
        else:
            st.line_chart(lb_hist.set_index("build_date")["sn_lift_vs_agentic_cio"])
            st.dataframe(lb_hist, use_container_width=True, hide_index=True)

    with st.expander("Raw audit record"):
        if not audit_dates:
            st.write("No audit records yet.")
        else:
            pick = st.selectbox(
                "Audit date", list(reversed(audit_dates)), key="champion_audit_pick",
            )
            picked = load_champion_audit(pick)
            if isinstance(picked, dict):
                st.json(picked)


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

producer_tab, scanner_tab, champion_tab = st.tabs(
    ["Producer ablation", "Scanner ablation", "Champion loop"],
)

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

with champion_tab:
    _render_champion_loop()
