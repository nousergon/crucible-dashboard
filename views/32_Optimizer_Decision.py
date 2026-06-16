"""
Optimizer Decision — Alpha Engine (private console)

The per-stock "why this weight?" view of the daily MVO portfolio optimizer.
For one cycle it reads the full optimizer shadow log
(``predictor/optimizer_shadow/{date}.json``, written by the executor's morning
planner) and shows, per ticker: expected alpha (α̂) and its uncertainty (σ_α̂),
eligibility + reason, current → target weight + Δ, the share of portfolio
variance the position contributes (risk contribution, computed live from the
persisted daily covariance matrix), the stance cap, and which constraint is
binding the weight.

Complements ``30_Optimizer_Risk`` (the time-series of levers + portfolio risk
metrics) and ``16_Order_Book_Rationale`` (the per-ticker terminal-state chain):
this page is the per-name sizing microscope.

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome — no set_page_config (the st.navigation entrypoint in app.py owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import streamlit as st

from loaders.s3_loader import list_optimizer_shadow_dates, load_optimizer_shadow
from loaders.utils import risk_contribution_shares

# Pseudo-sectors the producer assigns to the benchmark fill + cash sleeve.
_NON_EQUITY_SECTORS = {"__benchmark__", "__cash__"}
_EPS = 1e-4  # weight tolerance for "binding cap" detection (fractions, not %)

st.markdown("### 🎚 Optimizer Decision")
st.caption(
    "Why the MVO optimizer gave each stock the weight it did — expected alpha, "
    "uncertainty, risk contribution, and the binding constraint. Read from the "
    "daily optimizer shadow log (no LLM, no cost)."
)

dates = list_optimizer_shadow_dates()
if not dates:
    st.warning(
        "No optimizer shadow logs found under predictor/optimizer_shadow/. "
        "They're written every weekday by the executor morning planner."
    )
    st.stop()

run_date = st.selectbox("Cycle (run_date)", list(reversed(dates)), index=0)
shadow = load_optimizer_shadow(run_date)
if not shadow:
    st.error(f"Could not load optimizer_shadow/{run_date}.json.")
    st.stop()

if shadow.get("shadow_status") != "ok":
    st.warning(
        f"shadow_status = {shadow.get('shadow_status')!r} for this cycle — the "
        "optimizer solve failed or was skipped; fields below may be partial."
    )

cfg = shadow.get("optimizer_cfg") or {}
diag = shadow.get("diagnostics") or {}

# ---------------------------------------------------------------------------
# Cycle headline — portfolio-level solve result + deployed levers
# ---------------------------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Portfolio NAV", f"${shadow.get('portfolio_nav'):,.0f}"
          if isinstance(shadow.get("portfolio_nav"), (int, float)) else "—")
c2.metric("Solve status", str(diag.get("status", "—")))
c3.metric("Expected α (wᵀα̂)", f"{diag.get('expected_alpha'):.4f}"
          if isinstance(diag.get("expected_alpha"), (int, float)) else "—")
c4.metric("Active share vs SPY", f"{diag.get('active_share_vs_spy'):.1%}"
          if isinstance(diag.get("active_share_vs_spy"), (int, float)) else "—")

c5, c6, c7, c8 = st.columns(4)
c5.metric("λ (risk aversion)", cfg.get("risk_aversion", "—"))
c6.metric("γ (α̂ uncertainty)", cfg.get("alpha_uncertainty_penalty", "—"))
c7.metric("Max sector %", f"{cfg.get('max_sector_pct'):.0%}"
          if isinstance(cfg.get("max_sector_pct"), (int, float)) else "—")
c8.metric("Turnover (one-way)", f"{diag.get('turnover_one_way'):.1%}"
          if isinstance(diag.get("turnover_one_way"), (int, float)) else "—")

if diag.get("turnover_capped") or diag.get("large_move_flagged"):
    st.info(
        "⚠️ Portfolio-level turnover constraint engaged this cycle "
        f"(turnover_capped={diag.get('turnover_capped')}, "
        f"large_move_flagged={diag.get('large_move_flagged')}) — some target "
        "weights were scaled back toward current after the unconstrained solve."
    )

st.divider()

# ---------------------------------------------------------------------------
# Build the per-ticker frame + risk contributions
# ---------------------------------------------------------------------------
tickers = shadow.get("tickers") or []
if not tickers:
    st.warning("No per-ticker arrays in this shadow log.")
    st.stop()

n = len(tickers)


def _arr(key, default=np.nan):
    v = shadow.get(key)
    if not isinstance(v, list) or len(v) != n:
        return [default] * n
    return v


tw = np.array([float(x) if isinstance(x, (int, float)) else 0.0 for x in _arr("target_weights", 0.0)])

df = pd.DataFrame({
    "ticker": tickers,
    "sector": _arr("sectors", None),
    "eligible": _arr("eligibility", None),
    "reason": _arr("eligibility_reasons", None),
    "alpha_hat": _arr("alpha_hat"),
    "alpha_uncertainty": _arr("alpha_uncertainty"),
    "current_w": _arr("current_weights", 0.0),
    "target_w": _arr("target_weights", 0.0),
    "stance_cap": _arr("stance_caps"),
})
df["delta_w"] = df["target_w"] - df["current_w"]

# Risk contribution from the persisted daily covariance (rc_i = w_i (Σw)_i ⁄
# wᵀΣw), as % of total portfolio variance. NaN when covariance is absent.
df["risk_contrib_pct"] = risk_contribution_shares(
    tw.tolist(), shadow.get("covariance_daily")
)

# Binding-constraint heuristic (per stock, on the realized target weight).
sector_tgt = df.groupby("sector")["target_w"].transform("sum")
max_sector = cfg.get("max_sector_pct")


def _binding(row) -> str:
    if row["sector"] in _NON_EQUITY_SECTORS:
        return "—"
    if not bool(row["eligible"]):
        return f"ineligible ({row['reason']})" if row["reason"] else "ineligible"
    if row["target_w"] <= _EPS:
        return "zero (no edge / dropped)"
    if isinstance(row["stance_cap"], (int, float)) and row["target_w"] >= row["stance_cap"] - _EPS:
        return "stance cap"
    return "interior"


df["binding"] = df.apply(_binding, axis=1)
if isinstance(max_sector, (int, float)):
    sector_bound = (sector_tgt >= max_sector - _EPS) & (~df["sector"].isin(_NON_EQUITY_SECTORS))
    # A sector-capped name that isn't already stance-bound is sector-bound.
    df.loc[sector_bound & (df["binding"] == "interior"), "binding"] = "sector cap"

# ---------------------------------------------------------------------------
# Universe table
# ---------------------------------------------------------------------------
st.markdown("#### Per-stock optimizer inputs & outputs")
st.caption(
    "Weights shown as % of NAV. `risk_contrib_pct` = share of total portfolio "
    "variance (wᵢ·(Σw)ᵢ ⁄ wᵀΣw) from the daily covariance. `binding` = which "
    "constraint is holding the weight (stance cap / sector cap / interior / "
    "zero / ineligible)."
)

show = df.copy()
for c in ("current_w", "target_w", "delta_w", "stance_cap"):
    show[c] = (show[c] * 100).round(2)
show["risk_contrib_pct"] = show["risk_contrib_pct"].round(2)
show["alpha_hat"] = show["alpha_hat"].round(4)
show["alpha_uncertainty"] = pd.to_numeric(show["alpha_uncertainty"], errors="coerce").round(4)
show = show.sort_values("target_w", ascending=False).reset_index(drop=True)

st.dataframe(
    show[["ticker", "sector", "eligible", "alpha_hat", "alpha_uncertainty",
          "current_w", "target_w", "delta_w", "risk_contrib_pct", "stance_cap",
          "binding"]],
    use_container_width=True, hide_index=True,
)

st.divider()

# ---------------------------------------------------------------------------
# Per-ticker drilldown
# ---------------------------------------------------------------------------
st.markdown("#### Single-stock breakdown")
sel = st.selectbox("Ticker", df["ticker"].tolist(), index=0)
r = df[df["ticker"] == sel].iloc[0]

d1, d2, d3 = st.columns(3)
d1.metric("Expected α̂ (21d log)", f"{r['alpha_hat']:.4f}"
          if pd.notna(r["alpha_hat"]) else "—")
au = pd.to_numeric(pd.Series([r["alpha_uncertainty"]]), errors="coerce").iloc[0]
d2.metric("σ_α̂ (uncertainty)", f"{au:.4f}" if pd.notna(au) else "n/a")
d3.metric("Risk contribution", f"{r['risk_contrib_pct']:.1f}%"
          if pd.notna(r["risk_contrib_pct"]) else "—")

e1, e2, e3 = st.columns(3)
e1.metric("Current weight", f"{r['current_w'] * 100:.2f}%")
e2.metric("Target weight", f"{r['target_w'] * 100:.2f}%")
e3.metric("Δ weight", f"{r['delta_w'] * 100:+.2f}%")

verdict = r["binding"]
if not bool(r["eligible"]):
    st.warning(f"**{sel}** was INELIGIBLE this cycle — reason: "
               f"`{r['reason'] or 'unspecified'}`. The optimizer could not give it weight.")
elif verdict == "stance cap":
    st.info(f"**{sel}** is **stance-cap bound** — the optimizer wanted more but "
            f"hit the per-name cap ({r['stance_cap'] * 100:.2f}% of NAV).")
elif verdict == "sector cap":
    st.info(f"**{sel}** is **sector-cap bound** — its sector ({r['sector']}) hit "
            f"the max_sector_pct ceiling, capping the name.")
elif verdict.startswith("zero"):
    st.info(f"**{sel}** got **zero weight** — insufficient risk-adjusted edge "
            "(α̂ net of uncertainty/cost did not clear the bar) or it was dropped.")
else:
    st.success(f"**{sel}** sits at an **interior** weight — sized by the α̂/risk "
               "trade-off, not pinned to any cap.")

with st.expander("Raw shadow-log fields for this ticker", expanded=False):
    st.dataframe(
        pd.DataFrame({"field": r.index, "value": r.values}),
        use_container_width=True, hide_index=True,
    )

st.caption(
    "Source: predictor/optimizer_shadow/{date}.json (executor morning planner). "
    "Risk contributions are computed at load time from the persisted daily "
    "covariance — they are not re-solved, so they reflect the realized target book."
)
