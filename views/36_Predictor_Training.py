"""
Predictor Training — Alpha Engine (private console)

The per-cycle base-retrain training summary, rendered in detail and across
time. Every Saturday the predictor retrains the base/meta model, runs the IC
gate + walk-forward validation, and either promotes or registers a challenger.
This page is the console home for that summary — the deep-link target of the
weekly training email (slimmed to a headline + link under config#856; the full
detail that used to live in the email body now lives HERE).

  1. one cycle's headline (model version, IC gate verdict, promotion status);
  2. walk-forward validation folds;
  3. feature importance (gain, and gain-vs-SHAP rank divergence when present);
  4. confidence calibration + per-feature IC / noise candidates.

Source: ``predictor/metrics/training_summary_{date}.json`` (the dumped training
``result`` dict, written by alpha-engine-predictor ``training/train_handler.py``
``_write_training_summary``). Read-only — no LLM call, no cost.

Complements ``7_Predictor`` (live-inference health + latest promotion state, off
the manifest) and ``35_Model_Zoo`` (weekly champion/challenger rotation). This
page is the per-cycle base-retrain record the training email describes.

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome — no set_page_config (the st.navigation entrypoint in app.py owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    list_predictor_training_dates,
    load_predictor_training_summary,
)


def _fmt(v, spec="{:+.4f}"):
    return spec.format(v) if isinstance(v, (int, float)) else "—"


st.markdown("### 🏋️ Predictor Training")
st.caption(
    "Per-cycle base-retrain summary — IC gate + walk-forward validation + "
    "promotion verdict, read from the recorded weekly training summaries "
    "(no LLM call, no cost). This is the full detail behind the slim training "
    "email (config#856)."
)

dates = list_predictor_training_dates()
if not dates:
    st.warning(
        "No training summaries found under predictor/metrics/"
        "training_summary_{date}.json. The predictor writes one on each weekly "
        "training cycle (train_handler.py::_write_training_summary)."
    )
    st.stop()

# Honor the ?date= deep-link from the slimmed training email
# (…/predictor-training?date=YYYY-MM-DD — the training cycle's trading-day key).
# Falls back to the latest cycle when absent or unknown. Mirrors the EOD Report /
# Model Zoo pages (config#856 deep-link contract).
options = dates  # already newest-first
qp_date = st.query_params.get("date")
default_idx = options.index(qp_date) if qp_date in options else 0
run_date = st.selectbox("Cycle (date)", options, index=default_idx)
st.query_params["date"] = run_date

r = load_predictor_training_summary(run_date)
if not r:
    st.error(f"Could not load the training summary for {run_date}.")
    st.stop()

# ---------------------------------------------------------------------------
# 1 · Headline — version / IC gate / promotion
# ---------------------------------------------------------------------------
version = r.get("model_version", "unknown")
is_meta = "meta" in str(version).lower()
passes_ic = bool(r.get("passes_ic_gate", False))
promoted = bool(r.get("promoted", False))
promoted_mode = r.get("promoted_mode")
auto_promote = bool(r.get("auto_promote_enabled", False))
challenger_registered = bool(passes_ic and not promoted and not auto_promote)

st.markdown(f"#### Cycle {run_date} — model `{version}`")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Model", str(version))
c2.metric("IC gate", "PASS ✓" if passes_ic else "FAIL ✗")
if promoted:
    _promo = f"Promoted ({promoted_mode})" if promoted_mode else "Promoted"
elif challenger_registered:
    _promo = "Challenger"
else:
    _promo = "Not promoted"
c3.metric("Promotion", _promo)
_elapsed = r.get("elapsed_s", 0) or 0
c4.metric("Train samples", f"{r.get('n_train', 0):,}",
          help=f"elapsed {_elapsed:.0f}s ({_elapsed/60:.1f} min)")

# Promotion verdict line — mirror the training email's authoritative
# gate-resolution readout (promotion_gate_detail.promoted_blocker_reason is
# computed off the SAME booleans the live `promoted` formula uses).
if promoted:
    st.success(
        f"**Promoted** → {'weights/meta/' if is_meta else f'gbm_latest ({promoted_mode})'}"
    )
elif challenger_registered:
    st.info(
        "**Registered challenger** — gates passed; the model-zoo `select_winner` "
        "step owns the promotion decision (challenger-first since config#1052/#679)."
    )
else:
    gate_detail = r.get("promotion_gate_detail") or {}
    blocker = gate_detail.get("promoted_blocker_reason")
    if blocker:
        st.warning(f"**Not promoted** — promotion blocked: `{blocker}`")
    else:
        st.warning("**Not promoted** — IC gate failed.")

# Core IC metrics.
st.markdown("**IC metrics**")
mcols = st.columns(5)
mcols[0].metric("Val IC", _fmt(r.get("val_ic")))
mcols[1].metric("Test IC", _fmt(r.get("test_ic")))
mcols[2].metric("MSE IC", _fmt(r.get("mse_ic", r.get("test_ic"))))
mcols[3].metric("Rank IC", _fmt(r.get("rank_ic")))
_ic_ir = r.get("ic_ir")
mcols[4].metric(
    "IC IR", _fmt(_ic_ir, "{:.3f}"),
    help=f"{r.get('ic_positive_20', 0)}/20 positive" if _ic_ir is not None else None,
)
if is_meta and r.get("meta_model_oos_ic") is not None:
    st.caption(f"Meta-Model OOS IC (Spearman): {_fmt(r.get('meta_model_oos_ic'))}")

st.divider()

# ---------------------------------------------------------------------------
# 2 · Walk-forward validation
# ---------------------------------------------------------------------------
st.markdown("#### Walk-forward validation")
wf = r.get("walk_forward") or {}
folds = wf.get("folds") or []
if not folds:
    st.caption("No walk-forward folds recorded for this cycle.")
else:
    wf_pass = wf.get("passes_wf")
    if is_meta:
        vol_median = wf.get("volatility_median_ic")
        mom_median = wf.get("momentum_median_ic")
        st.caption(
            f"Volatility median IC: **{_fmt(vol_median)}** · Status: "
            f"**{'PASS ✓' if wf_pass else 'FAIL ✗'}** · Momentum median IC "
            f"(observability, not gating): {_fmt(mom_median)}"
        )
    else:
        wf_median = wf.get("median_ic") or wf.get("momentum_median_ic")
        pct = wf.get("pct_positive")
        st.caption(
            f"Median IC: **{_fmt(wf_median)}** — "
            f"**{'PASS ✓' if wf_pass else 'FAIL ✗'}**"
            + (f" · Positive folds: **{pct*100:.0f}%**" if isinstance(pct, (int, float)) else "")
        )
    fdf = pd.DataFrame([f for f in folds if isinstance(f, dict)])
    # Defensive column selection — a fold can lack n_train/ic (errored or
    # CPCV-only summary); render whichever fields are present (config#1083).
    cols = [c for c in ("fold", "test_start", "test_end", "n_train", "ic",
                        "mom_ic", "vol_ic") if c in fdf.columns]
    if cols:
        st.dataframe(fdf[cols], use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# 3 · Feature importance (gain, and gain-vs-SHAP rank when present)
# ---------------------------------------------------------------------------
st.markdown("#### Feature importance")
top10 = r.get("feature_importance_top10") or []
shap_top10 = r.get("feature_importance_shap_top10") or []
shap_stability = r.get("shap_rank_stability")

if not top10:
    st.caption("No feature-importance record for this cycle.")
else:
    if shap_top10:
        if shap_stability is not None:
            _stab = "stable" if shap_stability >= 0.80 else "DRIFT WARNING"
            st.caption(f"SHAP rank stability (vs last week): rho={shap_stability:.4f} — {_stab}")
        gain_rank = {row["feature"]: i + 1 for i, row in enumerate(top10)
                     if isinstance(row, dict) and "feature" in row}
        shap_rank = {row["feature"]: i + 1 for i, row in enumerate(shap_top10)
                     if isinstance(row, dict) and "feature" in row}
        feats = list(dict.fromkeys(list(gain_rank) + list(shap_rank)))[:10]
        cmp_rows = []
        for feat in feats:
            g = gain_rank.get(feat, "—")
            s = shap_rank.get(feat, "—")
            divergent = (isinstance(g, int) and isinstance(s, int) and abs(g - s) > 3)
            cmp_rows.append({"feature": feat, "gain_rank": g, "shap_rank": s,
                             "divergent (>3)": "⚠️" if divergent else ""})
        st.dataframe(pd.DataFrame(cmp_rows), use_container_width=True, hide_index=True)
        st.caption("Features with gain-vs-SHAP rank divergence > 3 flagged ⚠️.")
    else:
        gdf = pd.DataFrame([row for row in top10 if isinstance(row, dict)])
        cols = [c for c in ("feature", "gain") if c in gdf.columns]
        if cols:
            st.dataframe(gdf[cols].head(10), use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# 4 · Calibration + feature health
# ---------------------------------------------------------------------------
cal = r.get("calibration") or {}
if cal and cal.get("fitted"):
    st.markdown("#### Confidence calibration")
    _eb = cal.get("ece_before")
    _ea = cal.get("ece_after")
    _reduction = (
        f" ({(1 - _ea / max(_eb, 1e-8)) * 100:.0f}% reduction)"
        if isinstance(_eb, (int, float)) and isinstance(_ea, (int, float)) else ""
    )
    st.caption(
        f"Method: **{cal.get('method', '—')}** · Samples: "
        f"**{cal.get('n_samples', 0):,}** · ECE: **{_fmt(_eb)} → {_fmt(_ea)}**{_reduction}"
    )

feat_ics = r.get("feature_ics") or {}
noise_cands = r.get("noise_candidates") or []
if feat_ics:
    st.markdown("#### Feature health (top by |IC|)")
    sorted_ics = sorted(feat_ics.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
    st.dataframe(
        pd.DataFrame(sorted_ics, columns=["feature", "IC vs forward"]),
        use_container_width=True, hide_index=True,
    )
    if noise_cands:
        st.warning(f"Noise candidates ({len(noise_cands)}): {', '.join(noise_cands)}")

st.caption(
    "Live-inference health + the promoted-model state are on the **Predictor** "
    "page; the weekly champion/challenger rotation is on **Model Zoo**. This "
    "page is the per-cycle base-retrain training record."
)
