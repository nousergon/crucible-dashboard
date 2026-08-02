"""Attractiveness Trends — Alpha Engine (private console)

Per-stock attractiveness OVER TIME + the weekly trajectory signal: which names
are **rising** (significant positive attractiveness trend) and, the alpha-rich
subset, **pre-repricing** (rising attractiveness whose price hasn't caught up to
its sector — the orthogonalized residual of attractiveness-momentum on
sector-neutral price-momentum). Reads the typed
``scanner/universe/trajectory/latest.json`` + the attractiveness-history parquet
produced by crucible-research (no LLM call, no cost).

OBSERVE-MODE: this is a measured signal whose forward IC is still being tracked —
read it as a research lens, not a trade instruction.

Lives on dashboard.nousergon.ai (Cloudflare Access-gated). Native Streamlit chrome
— no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.attractiveness_trends import (
    flatten_trajectory,
    pre_repricing_table,
    rising_table,
    ticker_series,
    trajectory_meta,
)
from loaders.s3_loader import load_attractiveness_history, load_attractiveness_trajectory

st.markdown("### 📈 Attractiveness Trends")
st.caption(
    "How each stock's attractiveness is moving over time, plus the weekly "
    "trajectory signal: **rising** names and **pre-repricing** names (rising "
    "attractiveness the price hasn't caught up to). Read from the recorded "
    "weekly history (no LLM call, no cost)."
)

artifact = load_attractiveness_trajectory()
history = load_attractiveness_history()

if not artifact or not artifact.get("stocks"):
    st.warning(
        "No trajectory signal published yet. It needs ≥4 weekly attractiveness "
        "cycles and is produced each Saturday by crucible-research "
        "(`scoring/attractiveness_trajectory.py`). The per-stock history below "
        "still renders once the history parquet exists."
    )
    if not history.empty:
        st.divider()
    else:
        st.stop()

meta = trajectory_meta(artifact)
df = flatten_trajectory(artifact)

# ── Headline ─────────────────────────────────────────────────────────────────
if not df.empty:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Universe", meta["n_universe"])
    m2.metric("Rising", meta["n_rising"])
    m3.metric("Pre-repricing", meta["n_pre_repricing"])
    m4.metric("As of", meta["as_of"])
    st.caption(
        f"Signal = {meta['method'].replace('_', ' ')} · {meta['window_weeks']}-week "
        f"Theil-Sen trend · sector-neutral price momentum · "
        f"observe-mode (forward IC: {meta.get('provisional_ic') or 'accruing'})."
    )
    st.divider()

# ── Leaderboards ─────────────────────────────────────────────────────────────
_LB_COLS = ["ticker", "sector", "pre_repricing_score", "attr_slope", "attr_slope_z",
            "sector_rel_price_ret", "price_mom_z", "n_points"]
_LB_CFG = {
    "pre_repricing_score": st.column_config.NumberColumn("Residual", format="%+.2f",
        help="Rising attractiveness NOT explained by price (orthogonalized). Higher = more pre-repricing."),
    "attr_slope": st.column_config.NumberColumn("Attr slope/wk", format="%+.3f"),
    "attr_slope_z": st.column_config.NumberColumn("Slope z", format="%+.2f"),
    "sector_rel_price_ret": st.column_config.NumberColumn("Price vs sector", format="percent",
        help="Stock's window return minus its sector ETF (sector-neutral price momentum)."),
    "price_mom_z": st.column_config.NumberColumn("Price z", format="%+.2f"),
    "n_points": st.column_config.NumberColumn("Pts", format="%d"),
}

if not df.empty:
    st.markdown("#### 🎯 Pre-repricing — rising attractiveness, price lagging sector")
    pre = pre_repricing_table(df)
    if pre.empty:
        st.caption("No pre-repricing names this cycle.")
    else:
        st.dataframe(pre[_LB_COLS], use_container_width=True, hide_index=True, column_config=_LB_CFG)
        st.caption("The residual isolates attractiveness improvement the market hasn't priced — "
                   "the lead-lag / under-reaction thesis. Observe-mode: validate against forward IC.")

    st.markdown("#### ⬆️ Rising attractiveness")
    rising = rising_table(df)
    if rising.empty:
        st.caption("No names with a significant positive attractiveness trend this cycle.")
    else:
        st.dataframe(rising[_LB_COLS], use_container_width=True, hide_index=True, column_config=_LB_CFG)
    st.divider()

# ── Per-stock attractiveness over time ───────────────────────────────────────
st.markdown("#### 🔬 Per-stock attractiveness over time")
if history.empty:
    st.caption("No attractiveness history parquet yet — it accrues one point per Saturday cycle "
               "(and was seeded by the one-time backfill).")
else:
    all_tickers = sorted(history["ticker"].dropna().unique().tolist())
    # default to the top pre-repricing name when available
    default_ix = 0
    if not df.empty:
        pre = pre_repricing_table(df)
        if not pre.empty and pre.iloc[0]["ticker"] in all_tickers:
            default_ix = all_tickers.index(pre.iloc[0]["ticker"])
    pick = st.selectbox("Ticker", all_tickers, index=default_ix, key="attr_trend_ticker")
    series = ticker_series(history, pick)
    if series.empty:
        st.caption("No history for this ticker.")
    else:
        st.line_chart(series[["attractiveness_score"]], height=280)
        st.caption("Attractiveness percentile (0–100) over the weekly cycles. "
                   "Raw z-blend also retained in the history.")
        if not df.empty:
            row = df[df["ticker"] == pick]
            if not row.empty:
                r = row.iloc[0]
                rel = r["sector_rel_price_ret"]
                rel_txt = "n/a" if pd.isna(rel) else f"{rel * 100:+.1f}% vs sector"
                flag = ("🎯 pre-repricing" if r["pre_repricing"] else
                        ("⬆️ rising" if r["rising"] else "—"))
                st.caption(
                    f"**{pick}** — attr slope {r['attr_slope']:+.3f}/wk (z {r['attr_slope_z']:+.2f}) · "
                    f"price {rel_txt} · residual {r['pre_repricing_score']:+.2f} · {flag}"
                )

st.caption(
    "Pre-repricing = the orthogonalized residual of attractiveness-momentum on "
    "sector-neutral price-momentum (rising attractiveness the price hasn't caught "
    "up to). Observe-mode signal — measured against forward IC before it's trusted."
)
