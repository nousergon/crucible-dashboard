"""
Model Zoo — Alpha Engine (private console)

The weekly champion/challenger rotation, in detail and across time. Each
Saturday the model-zoo trains the fresh champion-arch + the active challenger
specs, runs leak-free CPCV selection, and promotes under fresh-best-wins
(config#1068/#1083/#1088). This page shows:

  1. one cycle's full leaderboard + promotion verdict (serving champion vs
     fresh champion-arch baseline, per-spec CPCV IC + DSR gates, who won, the
     margin, what was promoted, PBO, and the realized-edge "chasing noise"
     monitor);
  2. the multi-week promotion trajectory; and
  3. the realized champion/challenger OOS rank-IC scorecard.

Complements ``7_Predictor`` (live-inference health + the latest rotation
snapshot) — this is the archival, cross-week zoo view. Sources:
``predictor/model_zoo/leaderboard/{date}.json`` + the predictor_outcomes
tables. No LLM call.

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
    get_model_version_scorecard,
    get_per_spec_realized_alpha_series,
)
from loaders.s3_loader import (
    list_model_zoo_leaderboard_dates,
    load_model_zoo_history,
    load_model_zoo_leaderboard,
)


def _fmt(v, spec="{:.4f}"):
    return spec.format(v) if isinstance(v, (int, float)) else "—"


st.markdown("### 🦓 Model Zoo")
st.caption(
    "Weekly champion/challenger rotation — leak-free CPCV selection + "
    "fresh-best-wins promotion. Per-cycle detail, the multi-week promotion "
    "trajectory, and the realized scorecard. Read from the recorded "
    "leaderboards (no LLM call, no cost)."
)

dates = list_model_zoo_leaderboard_dates()
if not dates:
    st.warning(
        "No model-zoo leaderboards found under predictor/model_zoo/leaderboard/. "
        "The rotation writes one on each Saturday cycle (config#1083)."
    )
    st.stop()

run_date = st.selectbox("Cycle (date)", list(reversed(dates)), index=0)
lb = load_model_zoo_leaderboard(run_date)
if not lb:
    st.error(f"Could not load the leaderboard for {run_date}.")
    st.stop()

# ---------------------------------------------------------------------------
# 1 · Selected-cycle detail
# ---------------------------------------------------------------------------
st.markdown(f"#### Cycle {lb.get('date', run_date)} — mode: `{lb.get('mode', '?')}`")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Baseline IC", _fmt(lb.get("promotion_baseline_ic")),
          help=f"source: {lb.get('promotion_baseline_source', '—')}")
cands = lb.get("candidates") or []
winner_id = lb.get("winner_version_id")
winner_ic = next((c.get("cpcv_mean_ic") for c in cands
                  if isinstance(c, dict) and c.get("version_id") == winner_id), None)
c2.metric("Winner IC", _fmt(winner_ic))
c3.metric("Margin", _fmt(lb.get("margin")))
c4.metric("Promoted", "—" if not lb.get("promoted") else (lb.get("promoted_kind") or "yes"))

# Promotion verdict line.
if lb.get("promoted"):
    st.success(
        f"**Promoted** `{lb.get('promoted')}` ({lb.get('promoted_kind') or 'n/a'})"
        + (f" — reverted from `{lb.get('reverted_from')}`" if lb.get("reverted_from") else "")
    )
else:
    st.info(
        "No promotion this cycle — "
        + ("observe mode (no live cutover)." if lb.get("mode") == "observe"
           else "no challenger cleared champion-arch + margin (champion-arch refresh only).")
    )

# Champion identity (serving = live/stale snapshot; champion_arch = fresh retrain).
sc = lb.get("serving_champion") or {}
ca = lb.get("champion_arch") or {}
e1, e2 = st.columns(2)
e1.metric("Serving champion (live)", _fmt(sc.get("cpcv_mean_ic")),
          help=f"{sc.get('served_version', '—')} · served {sc.get('served_date', '—')} "
               f"· stale snapshot: {sc.get('cpcv_is_stale_snapshot')}")
e2.metric("Champion-arch (fresh retrain)", _fmt(ca.get("cpcv_mean_ic")),
          help=f"{ca.get('version_id', '—')} — the promotion baseline")

# Guardrails: PBO + chasing-noise monitor.
pbo = lb.get("selection_pbo") or {}
mon = lb.get("champion_realized_monitor") or {}
g1, g2, g3 = st.columns(3)
g1.metric("Selection PBO", _fmt(pbo.get("pbo"), "{:.2f}"),
          help=f"target ≤ {pbo.get('pbo_target')} · pass: {pbo.get('pbo_pass')} "
               f"· status: {pbo.get('status')}")
g2.metric("Cumulative trials", lb.get("n_trials_cumulative", "—"))
cn = mon.get("chasing_noise")
g3.metric("Chasing noise?", {True: "⚠️ yes", False: "no", None: "n/a"}.get(cn, str(cn)),
          help=f"champion realized rank-IC {_fmt(mon.get('realized_rank_ic'))} over "
               f"{mon.get('n_matured_outcomes', '—')} matured outcomes")

# Candidate leaderboard.
st.markdown("**Candidates**")
if cands:
    cdf = pd.DataFrame([c for c in cands if isinstance(c, dict)])
    cols = ["spec_id", "group", "version_id", "forward_days", "cpcv_mean_ic",
            "dsr_training", "dsr_selection", "dsr_selection_n_eff", "passes_gate",
            "dsr_gate_pass", "registry_bar_pass", "beats_baseline_by_margin",
            "eligible", "reason"]
    cdf = cdf[[c for c in cols if c in cdf.columns]]
    if "cpcv_mean_ic" in cdf.columns:
        cdf = cdf.sort_values("cpcv_mean_ic", ascending=False)
    st.dataframe(cdf, use_container_width=True, hide_index=True)
else:
    st.caption("No candidates recorded for this cycle.")

st.divider()

# ---------------------------------------------------------------------------
# 2 · Multi-week promotion history
# ---------------------------------------------------------------------------
st.markdown("#### Promotion history")
hist = load_model_zoo_history(limit=26)
if not hist:
    st.caption("No prior rotations recorded yet.")
else:
    hdf = pd.DataFrame(hist)
    for col in ("baseline_ic", "winner_ic", "margin", "pbo"):
        if col in hdf.columns:
            hdf[col] = pd.to_numeric(hdf[col], errors="coerce").round(4)
    hdf["promoted?"] = hdf["promoted"].apply(lambda v: "✅" if v else "—")
    show_cols = ["date", "mode", "baseline_ic", "winner_ic", "margin", "promoted?",
                 "promoted_kind", "n_eligible", "n_candidates", "pbo", "chasing_noise"]
    st.dataframe(hdf[[c for c in show_cols if c in hdf.columns]],
                 use_container_width=True, hide_index=True)
    st.caption("Newest first. `promoted_kind` = challenger | champion-arch-refresh. "
               "`chasing_noise` flags when relative-best selection isn't tracking realized edge.")

st.divider()

# ---------------------------------------------------------------------------
# 3 · Realized champion/challenger scorecard
# ---------------------------------------------------------------------------
st.markdown("#### Realized scorecard (out-of-sample)")
st.caption(
    "Per model-version cross-sectional rank-IC (Fama-MacBeth) + hit-rate from "
    "resolved predictions — champion (predictor_outcomes) + challengers "
    "(predictor_outcomes_shadow). Empty until outcomes mature."
)
score = get_model_version_scorecard()
if score is None or score.empty:
    st.caption("No resolved outcomes yet — the scorecard fills as predictions mature (~21d).")
else:
    st.dataframe(score, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# 4 · Per-spec rolling realized-α (noise monitor)  — config#1079
# ---------------------------------------------------------------------------
# The trajectory companion to the point-in-time scorecard above: each spec's
# (model_version's) rolling realized 21d log-alpha over time, next to the
# rotation leaderboard. Observability-only — promotion ranks by leak-free CPCV
# mean IC (predictor #268), so this is a noise monitor, not a gate.
st.markdown("#### Rolling realized-α by spec")
st.caption(
    "Per-spec rolling mean of realized 21d log-alpha (8-date window) — the "
    "trajectory of each champion/challenger version's realized edge. "
    "Observability-only noise monitor; promotion ranks by leak-free CPCV "
    "mean IC, not this series. Empty until outcomes mature (~21d)."
)
realized_series = get_per_spec_realized_alpha_series()
if realized_series is None or realized_series.empty:
    st.caption("No resolved outcomes yet — the series fills as predictions mature (~21d).")
else:
    # One line per spec; legend = "stage · model_version" so champion and
    # challenger versions are distinguishable at a glance.
    chart_df = realized_series.copy()
    chart_df["series"] = chart_df["stage"] + " · " + chart_df["model_version"].astype(str)
    pivot = chart_df.pivot_table(
        index="prediction_date",
        columns="series",
        values="rolling_realized_alpha",
        aggfunc="last",
    ).sort_index()
    st.line_chart(pivot, height=320)
    st.caption(
        f"{realized_series['model_version'].nunique()} spec(s) · "
        f"{realized_series['prediction_date'].nunique()} date(s). "
        "Values are mean log-alpha across each spec's resolved picks per date, "
        "smoothed over an 8-date rolling window."
    )

st.caption(
    "Live-inference health + the L1/L2 IC decomposition are on the **Predictor** "
    "page; this page is the cross-week rotation/promotion record."
)
