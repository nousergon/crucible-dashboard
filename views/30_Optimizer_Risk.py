"""
Alpha Engine — Optimizer Risk (private console)

Time-series of the portfolio optimizer's **deployed risk-tolerance levers** and
the **live book's realized risk metrics**, one point per trading day, sourced
from the daily optimizer shadow log (`predictor/optimizer_shadow/{date}.json`)
— the definitive record of what actually shaped the book each day.

- **Levers** come from the shadow log's `optimizer_cfg` — the config the live
  optimizer actually used (defaults → risk.yaml → the MVO tuner's
  `config/portfolio_optimizer.json`, which *wins*). So when the backtester's
  `risk_aversion × tcost_bps` tuner promotes a new value, these lines move. They
  are flat while the deployed config is unchanged — the honest picture.
- **Risk metrics** come from the shadow log's `diagnostics` — the live book's
  realized posture: annualized portfolio vol, active share vs SPY, one-way
  turnover, expected alpha, active-position count.

Lives on dashboard.nousergon.ai (Cloudflare Access-gated). Showing specific lever
values is fine here — the disclosure boundary gates *public* surfaces.
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import load_optimizer_risk_history

_BLUE = "#1a73e8"
_GREEN = "#7fd17f"

# Deployed levers (from optimizer_cfg) → sparkline grid. (key, label)
_NUMERIC_LEVERS = [
    ("risk_aversion", "Risk aversion (λ) — ↓ = more risk"),
    ("tcost_bps", "Transaction-cost penalty (τ, bps)"),
    ("alpha_uncertainty_penalty", "α̂-uncertainty penalty (γ)"),
    ("sigma_horizon_days", "Σ horizon (days)"),
    ("max_daily_turnover", "Max daily turnover"),
    ("max_sector_pct", "Max sector weight"),
    ("cash_sleeve_pct", "Cash sleeve"),
    ("vol_target_annual", "Vol target (annual; blank = uncapped)"),
]
# Live-book risk metrics (from diagnostics) → full-width lines. (key, label)
_METRICS = [
    ("portfolio_vol_ann", "Portfolio volatility (annualized)"),
    ("active_share_vs_spy", "Active share vs SPY"),
    ("turnover_one_way", "One-way turnover (per rebalance)"),
    ("expected_alpha", "Expected alpha (wᵀα̂, 21d)"),
    ("n_active_positions", "Active positions"),
]


def _x_axis(df: pd.DataFrame):
    if "run_date" in df.columns:
        x = pd.to_datetime(df["run_date"], errors="coerce")
        if x.notna().any():
            return x
    return pd.Series(range(len(df)))


def _sparkline(df, x, key, label) -> None:
    if key not in df.columns:
        st.caption(f"{label}: —")
        return
    y = pd.to_numeric(df[key], errors="coerce")
    if y.notna().sum() == 0:
        st.caption(f"{label}: n/a (uncapped/unset)")
        return
    fig = go.Figure(go.Scatter(x=x, y=y, mode="lines+markers",
                               line=dict(color=_BLUE, width=2), marker=dict(size=7)))
    fig.update_layout(
        title=dict(text=label, font=dict(size=12)), height=170,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"optrisk_lever_{key}")


def _metric_line(df, x, key, label) -> None:
    if key not in df.columns:
        return
    y = pd.to_numeric(df[key], errors="coerce")
    if y.notna().sum() == 0:
        return
    fig = go.Figure(go.Scatter(x=x, y=y, mode="lines+markers",
                               line=dict(color=_GREEN, width=2), marker=dict(size=8)))
    fig.update_layout(
        title=label, height=240, margin=dict(l=10, r=10, t=40, b=20),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"optrisk_metric_{key}")


st.divider()
st.markdown("### Optimizer Risk")
st.markdown(
    "Deployed risk-tolerance levers of the daily MVO portfolio optimizer and the "
    "live book's realized risk, one point per trading day (source: the optimizer "
    "**shadow log** — exactly what shaped the book that day)."
)
st.caption("Source: `s3://alpha-engine-research/predictor/optimizer_shadow/{date}.json`")

history = load_optimizer_risk_history()

if not history:
    st.info(
        "No optimizer shadow-log records found. The executor's morning planner "
        "writes `predictor/optimizer_shadow/{date}.json` each weekday — this "
        "populates once the daily pipeline has run."
    )
    st.stop()

df = pd.DataFrame(history)
if "run_date" in df.columns:
    df = df.sort_values("run_date").reset_index(drop=True)
x = _x_axis(df)
latest = df.iloc[-1].to_dict()

st.markdown(
    f"**{len(df)} trading-day snapshot(s)** · latest `{latest.get('run_date', '?')}` "
    f"· status `{latest.get('shadow_status', '?')}` · "
    f"{latest.get('n_tickers', '?')} tickers considered"
)
st.caption(
    "Levers reflect the config the live optimizer actually used "
    "(`config/portfolio_optimizer.json` from the MVO tuner wins over risk.yaml "
    "over code defaults) — they move when the tuner promotes a change. To take "
    "on more risk, lower `risk_aversion` (λ). Metrics are the live book's "
    "realized posture and move every day."
)

# ── Current posture ──────────────────────────────────────────────────────────
st.divider()
st.markdown("#### Current posture (latest day)")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Risk aversion (λ)", latest.get("risk_aversion"))
c2.metric("Tcost (τ, bps)", latest.get("tcost_bps"))
c3.metric("Cov estimator", latest.get("covariance_shrinkage") or "—")
c4.metric("Vol target", latest.get("vol_target_annual") if latest.get("vol_target_annual") is not None else "uncapped")
c5, c6, c7, c8 = st.columns(4)
c5.metric("Portfolio vol (ann.)", f"{latest['portfolio_vol_ann']:.1%}" if latest.get("portfolio_vol_ann") is not None else "—")
c6.metric("Active share", f"{latest['active_share_vs_spy']:.1%}" if latest.get("active_share_vs_spy") is not None else "—")
c7.metric("Turnover (1-way)", f"{latest['turnover_one_way']:.1%}" if latest.get("turnover_one_way") is not None else "—")
c8.metric("Active positions", latest.get("n_active_positions"))

# ── Levers over time ─────────────────────────────────────────────────────────
st.divider()
st.markdown("#### Deployed risk-tolerance levers over time")
cols = st.columns(2)
for i, (key, label) in enumerate(_NUMERIC_LEVERS):
    with cols[i % 2]:
        _sparkline(df, x, key, label)

# ── Live-book risk metrics over time ─────────────────────────────────────────
st.divider()
st.markdown("#### Live-book risk metrics over time")
for key, label in _METRICS:
    _metric_line(df, x, key, label)

# ── Raw records ──────────────────────────────────────────────────────────────
st.divider()
with st.expander("Raw records", expanded=False):
    st.dataframe(df, use_container_width=True, hide_index=True)
