"""
Regime — Alpha Engine (private console)

Observability for the quantitative regime substrate (v3) produced
weekly by the Saturday SF ``RegimeSubstrate`` Lambda
(``alpha-engine-predictor-regime-substrate``). The substrate informs
the macro economist agent as a strong prior (Stage C, pending); the
macro agent remains the final regime authority.

Stage A is observe-only. This page is the primary surface for the
4-week observation window — operators verify HMM stability, calibration
sanity, and quant-vs-LLM disagreement before any downstream consumer
is wired to the substrate.

Surfaces shipped here:

- Current-week summary card (HMM argmax + intensity + change signal)
- HMM-vs-macro-agent disagreement check (substrate ``hmm.argmax`` vs
  ``signals.json`` ``market_regime``, mapped to the 3-state taxonomy)
- HMM probability trend (P(bear) / P(neutral) / P(bull) over time)
- Composite intensity_z trend (positive = risk-on)
- BOCPD change-signal markers + run-length confidence
- Per-feature z-scores (current week) + raw feature values
- Guardrail flag panel (mirrors macro agent's _validate_regime severity)
- Fit-window metadata + HMM feature columns
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import (
    load_drawdown_leg_history,
    load_drawdown_leg_latest,
    load_fast_signal_latest,
    load_regime_retrospective_eval_history,
    load_regime_retrospective_eval_latest,
    load_regime_stratified_sortino_history,
    load_regime_stratified_sortino_latest,
    load_regime_substrate_history,
    load_regime_substrate_latest,
)
from loaders.signal_loader import get_available_signal_dates, load_signals
from loaders.outcome_store import PRIMARY_HORIZON_DAYS

# config#1456 retired the 10d/30d eval horizons; 21d is now primary and 5d
# is the diagnostic horizon (nousergon_lib.quant.horizons.DEFAULT_POLICY).
# T2's producer (crucible-backtester's regime_stratified_sortino_runner,
# crucible-backtester#428) follows the same rename the rest of the outcome
# pipeline made — spread_10d/spread_30d became spread_21d/spread_5d.
from nousergon_lib.quant.horizons import DEFAULT_POLICY as _HORIZON_POLICY

_T2_DIAGNOSTIC_HORIZON_DAYS = _HORIZON_POLICY.diagnostic_horizons[0]




st.divider()

st.markdown("### Regime Substrate (v3) — Observation Console")
st.caption(
    "Quantitative regime substrate produced weekly by the Saturday SF "
    "``RegimeSubstrate`` Lambda. Substrate informs the macro economist "
    "agent as a strong prior; the macro agent remains the final regime "
    "authority. Stage A is observe-only — use this page to verify HMM "
    "stability + calibration + quant-vs-LLM agreement before wiring "
    "downstream consumers (Stage C onward)."
)

# ---------------------------------------------------------------------------
# Data load
# ---------------------------------------------------------------------------

latest = load_regime_substrate_latest()
history = load_regime_substrate_history(n_weeks=26)

if latest is None:
    st.warning(
        "No regime substrate artifact found at ``s3://alpha-engine-research/"
        "regime/latest.json``. This is expected before the first Saturday "
        "SF ``RegimeSubstrate`` state executes successfully. Verify the "
        "Lambda exists (``alpha-engine-predictor-regime-substrate``) and "
        "the SF state insertion has been deployed."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Current-week summary card
# ---------------------------------------------------------------------------

st.markdown("### Current week")

hmm = latest.get("hmm", {})
composite = latest.get("composite", {})
bocpd = latest.get("bocpd", {})

c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "HMM regime",
    str(hmm.get("argmax", "—")).upper(),
    delta=f"{hmm.get('weeks_in_current_state', 0)}w in state",
    delta_color="off",
)
c2.metric(
    "Intensity (risk-on → +)",
    f"{composite.get('intensity_z', 0.0):+.2f}",
    delta=composite.get("implied_severity", "—"),
    delta_color="off",
)
change_signal = bool(bocpd.get("change_signal", False))
c3.metric(
    "Change signal",
    "⚠ FIRED" if change_signal else "STABLE",
    delta=f"max run-length P={bocpd.get('max_runlength_prob', 0.0):.2f}",
    delta_color="off",
)
c4.metric(
    "Calendar / trading day",
    latest.get("calendar_date", "—"),
    delta=f"td: {latest.get('trading_day', '—')}",
    delta_color="off",
)

# Surface the HMM probability triplet inline
probs = hmm.get("probs", {})
st.caption(
    f"P(bear) = **{probs.get('bear', 0):.2f}**  ·  "
    f"P(neutral) = **{probs.get('neutral', 0):.2f}**  ·  "
    f"P(bull) = **{probs.get('bull', 0):.2f}**  ·  "
    f"argmax = **{hmm.get('argmax', '—')}**  ·  "
    f"run_id = `{latest.get('run_id', '—')}`"
)

st.divider()

# ---------------------------------------------------------------------------
# Fast signal (daily) — Stage F2 fast-vs-slow override observability
# ---------------------------------------------------------------------------

st.markdown("### Fast signal (daily) — forced-bear circuit-breaker")
st.caption(
    "Daily online BOCPD circuit-breaker (regime-fast-signal-260515.md), "
    "produced by the predictor's ``regime_fast_signal`` inference stage. "
    "Distinct cadence from the weekly substrate above. Observe-only "
    "until ``regime_forced_bear_enabled`` is flipped; this panel is the "
    "F2 parallel-observe surface — watch the fast-vs-slow disagreement "
    "before enabling the executor/veto override."
)

fast = load_fast_signal_latest()
if fast is None:
    st.info(
        "No fast-signal artifact at ``s3://alpha-engine-research/regime/"
        "fast_signal/latest.json`` yet — expected until the first daily "
        "``regime_fast_signal`` stage runs post-deploy (the detector also "
        "warms up over its first ~2 trading weeks)."
    )
else:
    forced = bool(fast.get("forced_bear", False))
    warmup = bool(fast.get("warmup", False))
    hmm_argmax = str(hmm.get("argmax", "")).lower()
    # Fast-vs-slow disagreement: the fast leg asserts bear while the
    # weekly HMM (slow leg) still reads non-bear. EXPECTED + desirable
    # when it happens (the fast leg's job is to fire ahead of the slow
    # leg) — but a persistent/frequent disagreement is the signal to
    # scrutinize before enabling the override.
    disagree = forced and hmm_argmax not in ("bear", "")

    f1, f2, f3, f4 = st.columns(4)
    f1.metric(
        "Forced bear",
        ("⚠ LATCHED" if forced else "clear") + (" (warmup)" if warmup else ""),
        delta=f"since {fast.get('forced_bear_since') or '—'}",
        delta_color="off",
    )
    f2.metric(
        "Change confidence",
        f"{fast.get('change_confidence', 0.0):.2f}",
        delta=f"consec break {fast.get('consecutive_change_days', 0)}d",
        delta_color="off",
    )
    f3.metric(
        "Fast intensity_z",
        f"{fast.get('intensity_z', 0.0):+.2f}",
        delta=f"hazard {fast.get('hazard', 0.0):.4f}",
        delta_color="off",
    )
    f4.metric(
        "Fast vs slow (HMM)",
        "⚠ DISAGREE" if disagree else "aligned",
        delta=f"fast={'bear' if forced else 'clear'} · slow={hmm_argmax or '—'}",
        delta_color="off",
    )

    if disagree:
        st.warning(
            f"**Fast-vs-slow disagreement active:** the daily fast signal "
            f"has latched `forced_bear` while the weekly HMM still reads "
            f"`{hmm_argmax or '—'}`. By design the fast leg fires *ahead* "
            f"of the slow leg — but verify this is a genuine break (not "
            f"whipsaw) before `regime_forced_bear_enabled` is flipped."
        )
    elif forced and warmup:
        st.info(
            "Fast signal indicates a break but the detector is still in "
            "its warmup window — `forced_bear` is suppressed by the "
            "producer and would not act even with the flag on."
        )
    st.caption(
        f"run_id = `{fast.get('run_id', '—')}`  ·  "
        f"trading_day = **{fast.get('trading_day', '—')}**  ·  "
        f"observed = {fast.get('observed', '—')}  ·  "
        f"cold_start = {fast.get('cold_start', '—')}"
    )

st.divider()

# ---------------------------------------------------------------------------
# Drawdown de-risk leg (daily) — 3rd ensemble leg, parallel-observe
# ---------------------------------------------------------------------------

st.markdown("### Drawdown de-risk leg (daily) — observe mode")
st.caption(
    "Deterministic pure-level hysteresis de-risk leg "
    "(regime-drawdown-hysteresis-260518.md) — the 3rd ensemble leg, "
    "produced daily by the predictor's ``regime_fast_signal`` stage "
    "``_advance_drawdown``. SPY market drawdown + book-vs-market excess "
    "compose with the HMM + Stage-F into a most-protective "
    "``effective_regime``. Observe-only until ``drawdown_regime_enabled`` "
    "is flipped — this panel is the parallel-observe counterfactual "
    "(would-be effective regime vs the drawdown-OFF baseline)."
)

dd = load_drawdown_leg_latest()
if dd is None:
    st.info(
        "No drawdown-leg artifact at ``s3://alpha-engine-research/regime/"
        "drawdown/latest.json`` yet — expected until the first daily "
        "``_advance_drawdown`` runs post-deploy."
    )
else:
    spy_leg = dd.get("spy", {}) or {}
    excess_leg = dd.get("excess", {}) or {}
    eff = dd.get("effective_regime", {}) or {}
    composed = str(eff.get("effective_regime", "—"))
    drivers = eff.get("drivers", {}) or {}

    # Drawdown-OFF baseline = most-protective over the NON-drawdown
    # drivers only (HMM + Stage-F forced_bear). The counterfactual delta
    # is composed-vs-baseline: what the discrete gates WOULD see if the
    # flag were on, vs what acts today (flag default-off).
    # 3-class Ang-Bekaert macro taxonomy (v0.42.0 / 2026-05-28 —
    # caution-regime-retirement-260528.md). Legacy 4-class "caution"
    # tier retired (its driving signals flow through regime_intensity_z
    # continuously); historical payloads carrying "caution" map to
    # severity 2 (between neutral and bear) for grandfather ordering.
    _ORDER = {"bull": 0, "neutral": 1, "caution": 2, "bear": 3}
    baseline_drivers = [
        drivers.get("hmm"), drivers.get("forced_bear"),
    ]
    present = [d for d in baseline_drivers if d]
    baseline = (
        max(present, key=lambda d: _ORDER.get(d, 1)) if present else "neutral"
    )
    would_change = composed != baseline

    spy_dd = spy_leg.get("drawdown")
    spy_dd_str = f"{-spy_dd * 100:.1f}%" if isinstance(spy_dd, (int, float)) else "—"
    excess_avail = bool(excess_leg.get("available"))
    excess_depth = excess_leg.get("excess_depth")
    excess_str = (
        f"{excess_depth * 100:.1f}pp"
        if excess_avail and isinstance(excess_depth, (int, float))
        else ("n/a" if not excess_avail else "—")
    )

    d1, d2, d3, d4 = st.columns(4)
    d1.metric(
        "SPY drawdown",
        spy_dd_str,
        delta=f"tier {spy_leg.get('tier', '—')}",
        delta_color="off",
    )
    d2.metric(
        "Book-vs-market excess",
        excess_str,
        delta=(
            f"tier {excess_leg.get('tier', '—')}" if excess_avail
            else "NAV unavailable — SPY leg only"
        ),
        delta_color="off",
    )
    d3.metric(
        "Composed effective",
        composed.upper(),
        delta=f"baseline (dd OFF) = {baseline}",
        delta_color="off",
    )
    d4.metric(
        "Counterfactual",
        "⚠ WOULD CHANGE" if would_change else "no delta",
        delta=f"{baseline} → {composed}" if would_change else "aligned",
        delta_color="off",
    )

    if would_change:
        st.warning(
            f"**Parallel-observe delta active:** with `drawdown_regime_enabled` "
            f"the discrete gates would see **{composed}** vs the current "
            f"drawdown-OFF baseline **{baseline}** — driven by "
            f"{', '.join(f'`{k}`={v}' for k, v in drivers.items() if v) or 'no escalating leg'}. "
            f"This is observed only; nothing acts on it until the flag is "
            f"flipped (gated on the skilled-risk counterfactual clearing)."
        )
    if excess_leg and not excess_avail:
        st.info(
            "Book-vs-market excess leg is **unavailable** (paper NAV "
            "short/gappy or not wired) — the SPY leg still acts; the "
            "excess leg contributes nothing to the composition."
        )
    st.caption(
        f"run_id = `{dd.get('run_id', '—')}`  ·  "
        f"trading_day = **{dd.get('trading_day', '—')}**  ·  "
        f"observed = {dd.get('observed', '—')}  ·  "
        f"cold_start = {dd.get('cold_start', '—')}"
    )

    # 2-week parallel-observe history.
    hist = load_drawdown_leg_history(n_days=14)
    if hist and len(hist) > 1:
        rows = []
        for a in hist:
            a_spy = a.get("spy", {}) or {}
            a_ex = a.get("excess", {}) or {}
            a_eff = (a.get("effective_regime", {}) or {})
            a_dd = a_spy.get("drawdown")
            a_drv = a_eff.get("drivers", {}) or {}
            a_present = [
                a_drv.get("hmm"), a_drv.get("forced_bear"),
            ]
            a_present = [d for d in a_present if d]
            a_base = (
                max(a_present, key=lambda d: _ORDER.get(d, 1))
                if a_present else "neutral"
            )
            a_composed = str(a_eff.get("effective_regime", "—"))
            rows.append({
                "Date": a.get("trading_day") or a.get("run_id", "—"),
                "SPY DD": (
                    f"{-a_dd * 100:.1f}%"
                    if isinstance(a_dd, (int, float)) else "—"
                ),
                "SPY tier": a_spy.get("tier", "—"),
                "Excess": (
                    f"{(a_ex.get('excess_depth') or 0) * 100:.1f}pp"
                    if a_ex.get("available") else "n/a"
                ),
                "Effective": a_composed,
                "Baseline (dd OFF)": a_base,
                "Δ": "yes" if a_composed != a_base else "—",
            })
        st.markdown("**2-week parallel-observe history (oldest → newest):**")
        st.dataframe(rows, use_container_width=True, hide_index=True)
    elif hist:
        st.caption(
            "_Single drawdown-leg artifact only — no 2-week history yet._"
        )

st.divider()

# ---------------------------------------------------------------------------
# HMM vs macro-agent disagreement (Stage A's headline diagnostic)
# ---------------------------------------------------------------------------

st.markdown("### HMM vs macro economist — disagreement check")
st.caption(
    "Substrate's HMM argmax compared to the macro agent's regime call "
    "from the most recent ``signals.json``. Both are calculated "
    "independently — the macro agent does not yet read the substrate "
    "(Stage C will wire it in). This panel is the observation-period "
    "instrument for measuring whether the LLM regime authority would "
    "agree, disagree by one severity, or disagree harder."
)

dates = get_available_signal_dates()
agent_regime: str | None = None
agent_date: str | None = None
if dates:
    agent_date = dates[0]
    signals = load_signals(agent_date)
    if signals:
        agent_regime = signals.get("market_regime")

if agent_regime is None:
    st.info("Macro agent regime call not yet available — `signals.json` for the latest date is missing or empty.")
else:
    # 3-class Ang-Bekaert macro taxonomy (v0.42.0 / 2026-05-28 —
    # caution-regime-retirement-260528.md). New emissions are 3-class
    # (bull/neutral/bear); historical signals.json artifacts predating
    # the cutover carry the legacy 4-class "caution" — map to "bear"
    # for grandfather attribution continuity (caution-tier stress had
    # higher protective severity than neutral, so the bear collapse
    # preserves the protective tilt at the cost of compressing a
    # half-step into a full bear posture). Caution-tilted severity for
    # new data is encoded continuously in regime_intensity_z (META_FEATURE
    # 13) and discretely in the predictor drawdown leg's
    # drawdown_protective_severity ordinal (axis ORTHOGONAL to macro).
    def _normalize(label: str) -> str:
        if label in ("bear", "caution"):
            return "bear"
        if label == "neutral":
            return "neutral"
        if label == "bull":
            return "bull"
        return label

    hmm_norm = _normalize(hmm.get("argmax", ""))
    agent_norm = _normalize(agent_regime)

    if hmm_norm == agent_norm:
        st.success(
            f"**Agreement.** HMM argmax = `{hmm.get('argmax')}` · "
            f"macro agent = `{agent_regime}` (signals.json from {agent_date})."
        )
    else:
        st.warning(
            f"**Disagreement.** HMM argmax = `{hmm.get('argmax')}` ≠ "
            f"macro agent = `{agent_regime}` (signals.json from {agent_date}). "
            f"This is informational during Stage A; not actionable yet. "
            f"Capture both calls + the realized market behavior over the "
            f"following 8 weeks for the T1 retrospective ground-truth "
            f"comparison (regime-v3-260514.md §5.3.3)."
        )

st.divider()

# ---------------------------------------------------------------------------
# HMM probability trend (stacked area)
# ---------------------------------------------------------------------------

if history:
    st.markdown("### HMM probability trend")
    st.caption(
        "Rolling weekly P(bear), P(neutral), P(bull) from the filter-only "
        "(Hamilton-Kim) posterior. Look for: (a) state stability — no "
        "label-switching across refits, (b) duration realism — bear/bull "
        "states lasting weeks-to-months not single weeks, (c) clean "
        "transitions during known regime shifts."
    )

    rows = []
    for entry in history:
        run_id = entry.get("run_id", "")
        ts = pd.to_datetime(entry.get("trading_day"), errors="coerce")
        p = entry.get("hmm", {}).get("probs", {})
        rows.append({
            "trading_day": ts,
            "P(bear)": p.get("bear", 0.0),
            "P(neutral)": p.get("neutral", 0.0),
            "P(bull)": p.get("bull", 0.0),
            "run_id": run_id,
        })
    hist_df = pd.DataFrame(rows).dropna(subset=["trading_day"]).sort_values("trading_day")

    if not hist_df.empty:
        fig = go.Figure()
        for col, color in [("P(bear)", "#d62728"), ("P(neutral)", "#7f7f7f"), ("P(bull)", "#2ca02c")]:
            fig.add_trace(go.Scatter(
                x=hist_df["trading_day"], y=hist_df[col],
                mode="lines", stackgroup="one", name=col,
                line=dict(width=0.5, color=color),
            ))
        fig.update_layout(
            height=320, margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(range=[0, 1], tickformat=".0%", title="Posterior"),
            xaxis_title="Trading day",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

    # Composite intensity trend
    st.markdown("### Composite intensity trend")
    st.caption(
        "AQR-style risk-on/risk-off macro z-score composite. Positive = "
        "risk-on; negative = risk-off. Pure rule-based (no estimation "
        "risk) — the always-available fallback when HMM is unfit. "
        "Threshold-band tints reflect ``implied_severity`` carving."
    )
    int_rows = []
    for entry in history:
        ts = pd.to_datetime(entry.get("trading_day"), errors="coerce")
        cz = entry.get("composite", {}).get("intensity_z")
        if ts is not None and cz is not None:
            int_rows.append({"trading_day": ts, "intensity_z": cz})
    int_df = pd.DataFrame(int_rows).dropna().sort_values("trading_day")
    if not int_df.empty:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=int_df["trading_day"], y=int_df["intensity_z"],
            mode="lines+markers", name="intensity_z",
            line=dict(color="#1f77b4", width=2),
        ))
        fig2.add_hline(y=1.0, line_dash="dot", line_color="#2ca02c", annotation_text="risk-on", annotation_position="right")
        fig2.add_hline(y=-1.0, line_dash="dot", line_color="#d62728", annotation_text="risk-off", annotation_position="right")
        fig2.add_hline(y=0.0, line_dash="solid", line_color="#cccccc")
        fig2.update_layout(
            height=300, margin=dict(l=0, r=0, t=10, b=0),
            yaxis_title="intensity_z (z-units)",
            xaxis_title="Trading day",
        )
        st.plotly_chart(fig2, use_container_width=True)

    # Change-signal markers
    change_rows = [
        (pd.to_datetime(e.get("trading_day"), errors="coerce"), bool(e.get("bocpd", {}).get("change_signal", False)))
        for e in history
    ]
    n_changes = sum(1 for _, c in change_rows if c)
    if n_changes:
        change_dates = [d.date() for d, c in change_rows if c and d is not pd.NaT]
        st.info(f"**BOCPD change-signal fired {n_changes}× in window** — dates: {', '.join(str(d) for d in change_dates)}")
    else:
        st.caption(f"BOCPD change-signal: no fires in the {len(history)}-week window.")

    st.divider()

# ---------------------------------------------------------------------------
# Current-week feature panel
# ---------------------------------------------------------------------------

st.markdown("### Current week features")
features = latest.get("features", {})
per_feature_z = composite.get("per_feature_z", {})

feature_rows = []
for feat in [
    "spy_20d_return", "vix_level", "vix_term_slope",
    "hy_oas_bps", "yield_curve_slope", "market_breadth",
]:
    raw = features.get(feat)
    z = per_feature_z.get(feat)
    feature_rows.append({
        "feature": feat,
        "raw_value": "—" if raw is None else f"{raw:.3f}",
        "z_score": "—" if z is None else f"{z:+.2f}",
    })
st.dataframe(pd.DataFrame(feature_rows), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# Guardrail flags
# ---------------------------------------------------------------------------

st.markdown("### Guardrail flags")
st.caption(
    "Mirrors the macro agent's ``_validate_regime`` severity-escalator "
    "rules. Active flags indicate quantitative conditions that would "
    "force a minimum severity if the macro agent were already consuming "
    "the substrate. ``active_severity_floor`` is the resulting minimum."
)
guardrails = latest.get("guardrails", {})
g_cols = st.columns(3)
flag_labels = [
    ("vix_caution_breached", "VIX caution"),
    ("vix_bear_breached", "VIX bear"),
    ("spy_30d_caution_breached", "SPY 30d caution"),
    ("spy_30d_bear_breached", "SPY 30d bear"),
    ("hy_oas_caution_breached", "HY OAS caution"),
]
for i, (k, label) in enumerate(flag_labels):
    fired = bool(guardrails.get(k))
    g_cols[i % 3].metric(label, "⚠ FIRED" if fired else "—", delta_color="off")
floor = guardrails.get("active_severity_floor")
if floor:
    st.warning(f"**Active severity floor:** `{floor}` — macro agent would be forced to at least this severity.")

# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------

st.divider()
st.markdown("### Model metadata")
md = latest.get("model_metadata", {})
md_cols = st.columns(3)
md_cols[0].caption(f"**HMM features:** `{', '.join(md.get('hmm_feature_columns') or [])}`")
md_cols[1].caption(f"**Fit window:** {md.get('fit_window_start', '—')} → {md.get('fit_window_end', '—')}")
md_cols[2].caption(f"**Written at:** {md.get('written_at', '—')}")
st.caption(f"Schema version: `{latest.get('schema_version', '—')}` · Composite weights: `{md.get('composite_weights_version', '—')}`")

st.divider()

# ---------------------------------------------------------------------------
# T1 — Retrospective HMM smoothing eval (regime-v3-260514 §5.3.3)
# ---------------------------------------------------------------------------
#
# Pairs each weekly macro-agent regime call against the HMM smoother's
# retrospective label (8-week lag — the smoother uses observations
# through t+8 to refine the posterior at t). Scores with asymmetric
# loss (bear-miss weighted 2× per the doc — calling bull/neutral when
# truth was bear is structurally worse than the reverse). Headline:
# 26-week rolling ``asymmetric_weighted_agreement_rate``.

st.markdown("### T1 — Retrospective HMM smoothing eval")
st.caption(
    "Tier 1 of the regime-v3 three-tier eval framework. Compares the "
    "macro economist's weekly regime call against the HMM smoother's "
    "retrospective label (8-week lag for posterior refinement). "
    "Asymmetric loss penalizes bear-misses 2× — calling bull/neutral "
    "when truth was bear is worse than the reverse."
)

t1_latest = load_regime_retrospective_eval_latest()

if t1_latest is None:
    st.info(
        "No T1 artifact at ``s3://alpha-engine-research/regime/retrospective/"
        "latest.json`` yet. Expected during the ~8-week cold-start window "
        "after the ``RegimeRetrospectiveEval`` Lambda first runs."
    )
else:
    t1_score = t1_latest.get("score", {})

    t1c1, t1c2, t1c3, t1c4 = st.columns(4)
    asym_rate = t1_score.get("asymmetric_weighted_agreement_rate")
    sym_rate = t1_score.get("symmetric_agreement_rate")
    rolling = t1_score.get("rolling_window_score")
    n_pairs = t1_score.get("n_pairings", 0)

    t1c1.metric(
        "Asymmetric agreement",
        f"{asym_rate:.1%}" if asym_rate is not None else "—",
        delta=f"symm: {sym_rate:.1%}" if sym_rate is not None else "—",
        delta_color="off",
    )
    t1c2.metric(
        f"Rolling {t1_score.get('rolling_window_weeks', 26)}w score",
        f"{rolling:.1%}" if rolling is not None else "—",
        delta=f"{t1_score.get('rolling_window_size', 0)} pairings in window",
        delta_color="off",
    )
    t1c3.metric(
        "Bear misses",
        f"{t1_score.get('bear_miss_count', 0)}",
        delta=f"weight = {t1_score.get('bear_miss_weight', 2.0)}×",
        delta_color="off",
    )
    t1c4.metric(
        "N pairings",
        f"{n_pairs}",
        delta=f"lag = {t1_latest.get('lag_weeks', 8)}w",
        delta_color="off",
    )

    # Rolling-score timeseries from history
    t1_history = load_regime_retrospective_eval_history(n_weeks=26)
    if t1_history:
        t1_rows = []
        for entry in t1_history:
            ts = pd.to_datetime(entry.get("trading_day"), errors="coerce")
            sc = entry.get("score", {}) or {}
            t1_rows.append({
                "trading_day": ts,
                "rolling_score": sc.get("rolling_window_score"),
                "asymmetric_rate": sc.get("asymmetric_weighted_agreement_rate"),
                "n_pairings": sc.get("n_pairings", 0),
            })
        t1_df = pd.DataFrame(t1_rows).dropna(subset=["trading_day"]).sort_values("trading_day")
        if not t1_df.empty:
            fig_t1 = go.Figure()
            fig_t1.add_trace(go.Scatter(
                x=t1_df["trading_day"],
                y=t1_df["rolling_score"],
                mode="lines+markers",
                name="rolling 26w (asym-weighted)",
                line=dict(color="#1f77b4", width=2),
            ))
            fig_t1.add_trace(go.Scatter(
                x=t1_df["trading_day"],
                y=t1_df["asymmetric_rate"],
                mode="lines",
                name="per-run (full window)",
                line=dict(color="#aaaaaa", width=1, dash="dot"),
            ))
            fig_t1.add_hline(y=0.5, line_dash="solid", line_color="#cccccc",
                             annotation_text="coin-flip baseline", annotation_position="right")
            fig_t1.update_layout(
                height=300, margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(range=[0, 1], tickformat=".0%", title="Agreement rate"),
                xaxis_title="Trading day",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig_t1, use_container_width=True)

    # Confusion matrix (last run)
    t1_confusion = t1_score.get("confusion_matrix") or {}
    if t1_confusion:
        st.caption("**Confusion matrix (most recent run)** — rows: agent call, columns: HMM retrospective label")
        cm_rows = []
        for agent_label, by_retro in t1_confusion.items():
            row = {"agent_call": agent_label}
            row.update({f"retro:{k}": v for k, v in (by_retro or {}).items()})
            cm_rows.append(row)
        if cm_rows:
            st.dataframe(pd.DataFrame(cm_rows), hide_index=True, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# T2 — Downstream-stratified Sortino (regime-v3-260514 §5.3.3)
# ---------------------------------------------------------------------------
#
# Validates whether the regime call leads to better downstream picks.
# Groups ``score_performance`` by ``market_regime`` (the agent's
# contemporaneous call at scoring time), computes Sortino + Sharpe +
# log-alpha + hit-rate per (regime, horizon) stratum. Headline:
# bull-Sortino minus bear-Sortino at the primary horizon. Positive spread =
# the regime call enabled better downside-risk-adjusted picks when
# bull-regime was declared vs when bear-regime was declared.

st.markdown("### T2 — Downstream-stratified Sortino")
st.caption(
    f"Tier 2 of the regime-v3 three-tier eval framework. Groups picks "
    f"by ``market_regime`` and measures whether the regime call "
    f"translated to better downstream outcomes. Headline = "
    f"bull-Sortino minus bear-Sortino at {PRIMARY_HORIZON_DAYS}d horizon; "
    f"positive = the regime label is signal-bearing for downstream pick quality."
)

t2_latest = load_regime_stratified_sortino_latest()

if t2_latest is None:
    st.info(
        "No T2 artifact at ``s3://alpha-engine-research/regime/"
        "stratified_sortino/latest.json`` yet. Expected after the first "
        "Saturday Backtester run that exercises the new "
        "``regime_stratified_sortino`` evaluate.py module."
    )
else:
    _primary_key = f"spread_{PRIMARY_HORIZON_DAYS}d"
    _diag_key = f"spread_{_T2_DIAGNOSTIC_HORIZON_DAYS}d"
    spread_primary = t2_latest.get(_primary_key, {}) or {}
    spread_diag = t2_latest.get(_diag_key, {}) or {}

    t2c1, t2c2, t2c3, t2c4 = st.columns(4)
    s_primary = spread_primary.get("spread_bull_minus_bear_sortino")
    s_diag = spread_diag.get("spread_bull_minus_bear_sortino")
    interp_primary = spread_primary.get("interpretation", "—")
    interp_diag = spread_diag.get("interpretation", "—")

    t2c1.metric(
        f"Spread {PRIMARY_HORIZON_DAYS}d (bull − bear)",
        f"{s_primary:+.2f}" if s_primary is not None else "—",
        delta=interp_primary,
        delta_color="off",
    )
    t2c2.metric(
        f"Spread {_T2_DIAGNOSTIC_HORIZON_DAYS}d (bull − bear)",
        f"{s_diag:+.2f}" if s_diag is not None else "—",
        delta=interp_diag,
        delta_color="off",
    )
    t2c3.metric(
        f"Bull Sortino ({PRIMARY_HORIZON_DAYS}d)",
        f"{spread_primary.get('bull_sortino'):.2f}" if spread_primary.get("bull_sortino") is not None else "—",
        delta=f"n = {spread_primary.get('bull_n_picks', 0)}",
        delta_color="off",
    )
    t2c4.metric(
        f"Bear Sortino ({PRIMARY_HORIZON_DAYS}d)",
        f"{spread_primary.get('bear_sortino'):.2f}" if spread_primary.get("bear_sortino") is not None else "—",
        delta=f"n = {spread_primary.get('bear_n_picks', 0)}",
        delta_color="off",
    )

    # Interpretation banner
    if interp_primary == "regime_signal_useful":
        st.success(
            f"**Regime signal is useful at {PRIMARY_HORIZON_DAYS}d horizon.** "
            f"Sortino spread {s_primary:+.2f} above the actionable threshold."
        )
    elif interp_primary == "regime_signal_inverted":
        st.warning(
            f"**Regime signal inverted at {PRIMARY_HORIZON_DAYS}d horizon.** "
            f"Sortino spread {s_primary:+.2f} suggests bear-declared picks "
            f"outperformed bull-declared — investigate before trusting the call."
        )
    elif interp_primary == "regime_signal_neutral":
        _neutral_spread = s_primary if s_primary else 0.0
        st.caption(
            f"Regime signal neutral at {PRIMARY_HORIZON_DAYS}d horizon "
            f"(spread {_neutral_spread:+.2f} in the no-signal band)."
        )
    else:
        st.caption(f"{PRIMARY_HORIZON_DAYS}d interpretation: {interp_primary}")

    # Per-stratum table
    strata = t2_latest.get("strata") or []
    if strata:
        strata_df = pd.DataFrame(strata)
        # Show only Sortino + Sharpe + n + hit-rate columns (terse)
        cols = [
            "market_regime", "horizon_days", "n_picks",
            "annualized_sortino", "annualized_sharpe_diagnostic",
            "mean_log_alpha", "hit_rate",
        ]
        cols = [c for c in cols if c in strata_df.columns]
        st.caption("**Per-stratum metrics**")
        st.dataframe(strata_df[cols], hide_index=True, use_container_width=True)

    # Rolling spread timeseries
    t2_history = load_regime_stratified_sortino_history(n_weeks=26)
    if t2_history:
        t2_rows = []
        for entry in t2_history:
            ts = pd.to_datetime(entry.get("trading_day"), errors="coerce")
            sp_primary = (entry.get(_primary_key) or {}).get("spread_bull_minus_bear_sortino")
            sp_diag = (entry.get(_diag_key) or {}).get("spread_bull_minus_bear_sortino")
            t2_rows.append({
                "trading_day": ts,
                _primary_key: sp_primary,
                _diag_key: sp_diag,
            })
        t2_df = pd.DataFrame(t2_rows).dropna(subset=["trading_day"]).sort_values("trading_day")
        if not t2_df.empty:
            fig_t2 = go.Figure()
            fig_t2.add_trace(go.Scatter(
                x=t2_df["trading_day"], y=t2_df[_primary_key],
                mode="lines+markers", name=f"spread {PRIMARY_HORIZON_DAYS}d",
                line=dict(color="#1f77b4", width=2),
            ))
            fig_t2.add_trace(go.Scatter(
                x=t2_df["trading_day"], y=t2_df[_diag_key],
                mode="lines+markers", name=f"spread {_T2_DIAGNOSTIC_HORIZON_DAYS}d",
                line=dict(color="#ff7f0e", width=2),
            ))
            fig_t2.add_hline(y=0.0, line_dash="solid", line_color="#cccccc")
            fig_t2.update_layout(
                height=300, margin=dict(l=0, r=0, t=10, b=0),
                yaxis_title="Sortino spread (bull − bear)",
                xaxis_title="Trading day",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig_t2, use_container_width=True)

