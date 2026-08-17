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
  tabs above — this is the GATED executor selection-path switch
  (``scanner_predictor_direct`` vs ``thinktank_coverage`` — the retired
  ``agentic`` seat left this rotation at the config-I2518 seat swap,
  2026-07-14). Shows the live pointer, weekly promotion/demotion audit
  history, per-arm gate state (the winner-take-all validity guards —
  feed-dead / leaderboard-stale / no-valid-selections / frozen — plus the
  retired HAC/hysteresis/cooldown vocabulary read-tolerated for pre-I2518
  audit history), and the challenger's weekly sector-neutral lift series.
  Read-only console surface; the pointer itself is written by
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
    # Arm 3 of 3 in the count-matched predictor universe (config-I4983) — its
    # shadow is written by the Think Tank's own daily run, not synthesised
    # during the weekly producer pass (registry.py build=None), but it is
    # scored by the leaderboard exactly like the other two challengers, so
    # it belongs in the same cohort/maturity accounting.
    "thinktank_coverage": "signals_shadow/thinktank_coverage/",
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
    # alpha-engine-config-I7542/I7549 — how much evidence stands behind the
    # row's mean ("ok" / "thin" / "insufficient", written by crucible-research
    # scoring/leaderboard_scoring.py::confidence_for). Rendered BESIDE the
    # mean, never instead of it: a one-date mean carrying a null se and a null
    # t_stat renders in exactly the same shape as a well-evidenced one, and a
    # reader (human or agent) has no way to tell them apart without this
    # column. Absent on pre-2026-08-17 artifacts — _spec_frame keeps only the
    # columns actually present, so those simply render without it.
    "confidence": "Evidence",
}

# Live champion/challenger rotation — MUST mirror alpha-engine-backtester's
# optimizer/champion_promotion.py::VALID_CHAMPIONS (config-I2518 seat swap,
# 2026-07-14: the "agentic" seat retired, "thinktank_coverage" joined).
# alpha-engine-config-I6431. Kept in sync by
# tests/test_experiments_page.py::TestChampionVocabularyParity.
_CHAMPION_ARMS = ("scanner_predictor_direct", "thinktank_coverage")
_CHAMPION_ARM_LABELS = {
    "scanner_predictor_direct": "Scanner → predictor (no agent)",
    "thinktank_coverage": "Think Tank coverage (per-ticker theses)",
    # RETIRED seat (config-I2518, 2026-07-14) — never a value in
    # _CHAMPION_ARMS above; kept only so a historical pointer/audit record
    # naming it (champion_promotion.py's _LEGACY_CHAMPIONS, read-tolerated)
    # renders a label instead of the raw slug.
    "agentic": "Agentic (RETIRED 2026-07-14)",
}
_BLOCKED_BY_LABELS = {
    # Current winner-take-all vocabulary (champion_promotion.py
    # _BLOCKED_BY_SLUGS, config-I2518/I2544/I2998).
    "no_valid_scanner_predictor_direct_selections": "no valid scanner→predictor selections",
    "no_valid_thinktank_coverage_selections": "no valid Think Tank coverage selections",
    "scanner_predictor_direct_counterfactual_unavailable": "scanner→predictor counterfactual unavailable",
    "thinktank_coverage_not_in_leaderboard": "Think Tank coverage not in leaderboard",
    "thinktank_coverage_no_resolved_outcomes": "Think Tank coverage has no resolved outcomes",
    "leaderboard_unavailable": "leaderboard unavailable",
    "leaderboard_stale_gt_8d": "leaderboard stale (>8d)",
    "arm_score_unavailable": "arm score unavailable",
    "feed_producer_dead": "feed producer dead (config-I3165)",
    # Evidence-admissibility verdicts (alpha-engine-config-I7549) — the THIRD
    # verdict: not "the challenger lost", not "the challenger won", but "the
    # evidence could not support a comparison". Rendered as its own phrase for
    # exactly that reason.
    "thinktank_coverage_thin_evidence":
        "Think Tank coverage scored on too few dates to compare (evidence thin)",
    "scanner_predictor_direct_thin_evidence":
        "scanner→predictor scored on too few cycles to compare (evidence thin)",
    "thinktank_coverage_confidence_unknown":
        "Think Tank coverage evidence unrated (pre-I7542 leaderboard)",
    "scanner_predictor_direct_confidence_unknown":
        "scanner→predictor evidence unrated (counterfactual reported no cycle count)",
    "frozen": "frozen (--freeze)",
    "unclassified_error": "error",
    # RETIRED pre-I2518 HAC/hysteresis/cooldown engine — read-tolerated for
    # historical audit records only; no live code path emits these anymore.
    "insufficient_matured_cohorts": "insufficient data (retired engine)",
    "cooldown_active": "cooldown (retired engine)",
    "not_significant_hac_adjusted": "not significant HAC-adjusted (retired engine)",
    "hysteresis_not_satisfied": "hysteresis pending (retired engine)",
    # RETIRED pre-I2544 exact-date-only leaderboard read — read-tolerated for
    # historical audit records only; superseded by leaderboard_stale_gt_8d.
    "leaderboard_stale": "leaderboard stale (retired engine)",
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


def _evidence_label(audit: dict) -> str:
    """Per-arm confidence behind this week's gate decision, from the audit
    record's ``evidence`` block (alpha-engine-config-I7549).

    Why this column exists: a no_contest, a defended incumbency and a week
    whose evidence was too thin to compare all leave the pointer where it was.
    Without this, all three render as "the pointer did not move", and "we
    could not tell" is indistinguishable from "the challenger lost" — the
    fleet's rule that no data is never rendered as green, run in the other
    direction. Empty string on pre-I7549 audit records, which carry no
    ``evidence`` block at all (never "ok", which would be a claim the record
    does not make)."""
    evidence = audit.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return ""
    parts = []
    for arm in _CHAMPION_ARMS:
        row = evidence.get(arm)
        if not isinstance(row, dict):
            continue
        verdict = row.get("confidence")
        if verdict is None:
            continue
        n = row.get("n_dates_scored", row.get("n_cycles"))
        label = _CHAMPION_ARM_LABELS.get(arm, arm)
        parts.append(f"{label}: {verdict}" + (f" (n={n})" if n is not None else ""))
    return "; ".join(parts)


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
            "Evidence": _evidence_label(audit),
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
        "this is the GATED executor selection-path switch "
        "(scanner_predictor_direct vs thinktank_coverage). A pointer move "
        "here changes what the live executor trades starting the next daily "
        "preopen run."
    )

    pointer = load_champion_pointer()
    # Base-case default mirrors champion_promotion.py's own
    # _normalize_champion_before: an absent/unrecognized pointer value
    # normalizes to VALID_CHAMPIONS[0] == "scanner_predictor_direct", not
    # the retired "agentic" seat.
    current_champion = (pointer or {}).get("champion", _CHAMPION_ARMS[0])

    if pointer is None:
        st.info(
            "No champion pointer written yet — the executor defaults to "
            f"'{_CHAMPION_ARM_LABELS[_CHAMPION_ARMS[0]]}' (pre-bootstrap)."
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
