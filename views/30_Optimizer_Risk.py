"""
Alpha Engine — Optimizer Risk (private console)

Time-series of the portfolio optimizer's **risk-tolerance levers** and the
**risk metrics** they produce, one record per backtester run. The MVO optimizer
(daily, cutover 2026-05-13) has a rich set of risk dials — variance penalty
(`risk_aversion`), transaction-cost penalty (`tcost_bps`), covariance estimator
+ horizon, α̂-uncertainty penalty (`alpha_uncertainty_penalty`), vol target,
turnover governor, cash-sleeve / sector caps. Each Saturday the backtester
sweeps several of them and posts a snapshot of the SELECTED configuration plus
its backtest risk metrics (Sortino / PSR / CVaR-95 / max-DD / tracking-error /
active-share / turnover).

Source: `config/optimizer_risk_history/{run_id}.json` — written by
`alpha-engine-backtester/optimizer/optimizer_risk_history.py`. The page reads
the append-per-run history and charts it over time.

Lives on console.nousergon.ai (Cloudflare Access-gated). Showing specific lever
values is fine here — the disclosure boundary gates *public* surfaces, not the
gated operator console.
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
_RED = "#b71c1c"

# Levers that are numeric → sparkline grid.
_NUMERIC_LEVERS = [
    ("risk_aversion", "Risk aversion (λ)"),
    ("tcost_bps", "Transaction-cost penalty (τ, bps)"),
    ("alpha_uncertainty_penalty", "α̂-uncertainty penalty (γ)"),
    ("sigma_horizon_days", "Σ horizon (days)"),
    ("max_daily_turnover", "Max daily turnover"),
    ("max_sector_pct", "Max sector weight"),
    ("cash_sleeve_pct", "Cash sleeve"),
    ("ewma_lambda_decay", "EWMA λ decay"),
]
# Risk metrics → full-width lines. (key, label, reference line, ref label)
_METRICS = [
    ("sortino_ratio", "Sortino ratio (primary skill metric)", 0.0, "0"),
    ("psr", "Probabilistic Sharpe Ratio (PSR)", 0.95, "0.95 gate"),
    ("max_drawdown", "Max drawdown", -0.35, "-0.35 floor"),
    ("cvar_95", "CVaR-95 (daily tail)", -0.05, "-0.05 floor"),
    ("tracking_error_ann", "Tracking error (annualized)", None, None),
    ("mean_active_share", "Mean active share vs SPY", None, None),
    ("turnover_one_way_ann", "Turnover (one-way, annualized)", None, None),
    ("sharpe_ratio", "Sharpe ratio", 0.0, "0"),
]


def _x_axis(df: pd.DataFrame) -> pd.Series:
    if "updated_at" in df.columns:
        x = pd.to_datetime(df["updated_at"], errors="coerce")
        if x.notna().any():
            return x
    return pd.Series(range(len(df)))


def _sparkline(df: pd.DataFrame, x, key: str, label: str) -> None:
    if key not in df.columns:
        st.caption(f"{label}: —")
        return
    y = pd.to_numeric(df[key], errors="coerce")
    if y.notna().sum() == 0:
        st.caption(f"{label}: n/a")
        return
    fig = go.Figure(
        go.Scatter(x=x, y=y, mode="lines+markers",
                   line=dict(color=_BLUE, width=2), marker=dict(size=7))
    )
    fig.update_layout(
        title=dict(text=label, font=dict(size=12)),
        height=170, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"optrisk_lever_{key}")


def _metric_line(df: pd.DataFrame, x, key: str, label: str,
                 ref: float | None, ref_label: str | None) -> None:
    if key not in df.columns:
        return
    y = pd.to_numeric(df[key], errors="coerce")
    if y.notna().sum() == 0:
        return
    fig = go.Figure(
        go.Scatter(x=x, y=y, mode="lines+markers",
                   line=dict(color=_GREEN, width=2), marker=dict(size=8))
    )
    if ref is not None:
        fig.add_hline(y=ref, line_dash="dot", line_color="#888",
                      annotation_text=ref_label, annotation_position="top left")
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
    "Risk-tolerance levers of the daily MVO portfolio optimizer and the risk "
    "metrics they produce, snapshotted on each backtester run. Each point is one "
    "run's **selected** configuration (the covariance-sweep winner, else the "
    "legacy baseline still in force)."
)
st.caption("Source: `s3://alpha-engine-research/config/optimizer_risk_history/{run_id}.json`")

history = load_optimizer_risk_history()

if not history:
    st.info(
        "No optimizer risk-history records yet. The backtester posts the first "
        "record on its next run that produces a covariance-estimator sweep "
        "(Saturday pipeline, or a manual `backtest.py --mode all`). The backfill "
        "script (`scripts/backfill_optimizer_risk_history.py`) seeds prior weeks."
    )
    st.stop()

df = pd.DataFrame(history)
# Stable chronological order: updated_at, then run_id as tiebreaker.
sort_cols = [c for c in ("updated_at", "run_id") if c in df.columns]
if sort_cols:
    df = df.sort_values(sort_cols).reset_index(drop=True)
x = _x_axis(df)

latest = df.iloc[-1].to_dict()
st.markdown(
    f"**{len(df)} run snapshot(s)** · latest `{latest.get('trading_day', '?')}` "
    f"· selected cell **{latest.get('cov_selected_name', '?')}** "
    f"({'sweep winner' if latest.get('cov_selected_is_winner') else 'baseline'})"
)

# ── Current posture ──────────────────────────────────────────────────────────
st.divider()
st.markdown("#### Current posture (latest run)")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Risk aversion (λ)", latest.get("risk_aversion"))
c2.metric("Cov estimator", latest.get("covariance_shrinkage") or "—")
c3.metric("Σ horizon (days)", latest.get("sigma_horizon_days"))
c4.metric("α̂ penalty (γ)", latest.get("alpha_uncertainty_penalty"))
c5, c6, c7, c8 = st.columns(4)
c5.metric("Sortino", round(latest["sortino_ratio"], 3) if latest.get("sortino_ratio") is not None else None)
c6.metric("PSR", round(latest["psr"], 3) if latest.get("psr") is not None else None)
c7.metric("Max drawdown", f"{latest['max_drawdown']:.1%}" if latest.get("max_drawdown") is not None else "—")
c8.metric("CVaR-95", f"{latest['cvar_95']:.2%}" if latest.get("cvar_95") is not None else "—")

gate = latest.get("gate_passed")
gate_txt = {True: "✅ gate passing", False: "⚠️ gate not passing", None: "gate n/a"}.get(gate, "gate n/a")
st.caption(
    f"Cutover gate (this run): {gate_txt} · γ-sweep status: "
    f"`{latest.get('gamma_status', '—')}`"
    + (f" (winner {latest['gamma_winner_name']})" if latest.get("gamma_winner_name") else "")
)

# ── Levers over time ─────────────────────────────────────────────────────────
st.divider()
st.markdown("#### Risk-tolerance levers over time")
cols = st.columns(2)
for i, (key, label) in enumerate(_NUMERIC_LEVERS):
    with cols[i % 2]:
        _sparkline(df, x, key, label)

# Covariance estimator is categorical — show its run-by-run sequence.
if "covariance_shrinkage" in df.columns:
    est_seq = df.assign(when=x)[["when", "covariance_shrinkage", "cov_selected_name"]]
    with st.expander("Covariance estimator selection per run", expanded=False):
        st.dataframe(est_seq, use_container_width=True, hide_index=True)

# ── Risk metrics over time ───────────────────────────────────────────────────
st.divider()
st.markdown("#### Risk metrics over time (selected configuration)")
for key, label, ref, ref_label in _METRICS:
    _metric_line(df, x, key, label, ref, ref_label)

# ── Raw records ──────────────────────────────────────────────────────────────
st.divider()
with st.expander("Raw records", expanded=False):
    st.dataframe(df, use_container_width=True, hide_index=True)
