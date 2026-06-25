"""Attribution Heatmaps — Alpha Engine (private console).

Security × time heatmaps of per-position performance, built from the executor's
EOD history (``trades/eod_pnl.csv`` → per-row ``positions_snapshot``). Four
matrices, daily and weekly:

  • ALPHA  — each held security's alpha per trading day / week
  • RETURN — each held security's total return per trading day / week

The ALPHA matrices toggle between two lenses (see ``shared.attribution``):
  • Market-relative return (position return − SPY return; unweighted)
  • NAV-weighted contribution (bps; sums to the portfolio's daily alpha)

Weekly matrices roll up by ISO week — returns compound geometrically over the
days held that week; the NAV-weighted contribution sums additively. Per-ticker
daily attribution populates ``eod_pnl`` from 2026-04-20 onward; earlier rows
lack per-position returns and are omitted.

This replaces the single-day attribution view as the cross-time picture of
which names are driving (and dragging) alpha — a single bad day (e.g. AMD
−5.8% on a −1.4% SPY day) reads as one red cell here, not the whole story.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import streamlit as st

from loaders.s3_loader import load_eod_pnl
from shared.attribution import (
    COL_CONTRIB_BPS,
    COL_RELATIVE,
    COL_RETURN,
    build_long_frame,
    build_weekly_frame,
    to_matrix,
)


def _heatmap(matrix: pd.DataFrame, *, color_label: str, value_fmt: str, unit: str):
    """Render a (ticker × period) matrix as a diverging RdYlGn heatmap anchored
    at zero (green = beat, red = lag)."""
    fig = px.imshow(
        matrix,
        color_continuous_scale="RdYlGn",
        color_continuous_midpoint=0.0,
        aspect="auto",
        text_auto=value_fmt,
    )
    fig.update_traces(
        hovertemplate=f"%{{y}} · %{{x}}<br>%{{z:{value_fmt}}}{unit}<extra></extra>"
    )
    fig.update_xaxes(side="top", tickangle=-45, title=None)
    fig.update_yaxes(title=None)
    fig.update_layout(
        height=max(280, 26 * len(matrix.index) + 150),
        margin=dict(t=110, b=20, l=70, r=20),
        plot_bgcolor="white",
        paper_bgcolor="white",
        coloraxis_colorbar=dict(title=color_label),
    )
    return fig


st.title("Attribution Heatmaps")
st.caption(
    "Per-security performance across time, from the executor's `eod_pnl` "
    "history. Rows = securities held (best performer on top); columns = trading "
    "day / week. Green = beat, red = lag. The cross-time picture that the "
    "single-day EOD attribution can't show."
)

eod_pnl = load_eod_pnl()
long_df = build_long_frame(eod_pnl)
if long_df.empty:
    st.info(
        "No per-position attribution history yet. `trades/eod_pnl.csv` carries "
        "per-ticker daily returns from 2026-04-20 onward; if this persists, the "
        "executor's EOD reconciliation may not be writing position snapshots."
    )
    st.stop()

weekly_df = build_weekly_frame(long_df)

# Alpha lens toggle — shared by the daily + weekly ALPHA heatmaps.
lens = st.radio(
    "Alpha lens",
    ["Market-relative return", "NAV-weighted contribution (bps)"],
    horizontal=True,
    help=(
        "Market-relative = position return − SPY return, unweighted "
        "(comparable across names regardless of size). NAV-weighted "
        "contribution (bps) sums across names to the portfolio's daily alpha — "
        "the EOD Report convention; a big mover in a small position shows a "
        "small cell."
    ),
)
if lens.startswith("Market-relative"):
    alpha_col, alpha_unit, alpha_fmt, alpha_clabel = COL_RELATIVE, "%", ".1f", "rel %"
else:
    alpha_col, alpha_unit, alpha_fmt, alpha_clabel = COL_CONTRIB_BPS, " bps", ".0f", "bps"

n_secs = long_df["ticker"].nunique()
n_days = long_df["date"].nunique()
n_weeks = weekly_df["week"].nunique() if not weekly_df.empty else 0
st.caption(
    f"{n_secs} securities · {n_days} trading days · {n_weeks} weeks · "
    f"{long_df['date'].min()} → {long_df['date'].max()}"
)

daily_tab, weekly_tab = st.tabs(["📅 Daily", "🗓 Weekly"])

with daily_tab:
    st.subheader(f"Daily Alpha — {lens}")
    m = to_matrix(long_df, alpha_col, period_col="date")
    if m.empty:
        st.warning("No data for this alpha lens.")
    else:
        st.plotly_chart(
            _heatmap(m, color_label=alpha_clabel, value_fmt=alpha_fmt, unit=alpha_unit),
            use_container_width=True,
        )

    st.subheader("Daily Total Return")
    mr = to_matrix(long_df, COL_RETURN, period_col="date")
    if mr.empty:
        st.warning("No return data.")
    else:
        st.plotly_chart(
            _heatmap(mr, color_label="ret %", value_fmt=".1f", unit="%"),
            use_container_width=True,
        )

with weekly_tab:
    if weekly_df.empty:
        st.info("Not enough history for a weekly view yet.")
    else:
        st.caption(
            "ISO weeks, labelled by the Monday. Returns compound geometrically "
            "over the days held that week; NAV-weighted contribution sums "
            "additively (daily contributions are additive by construction)."
        )
        st.subheader(f"Weekly Alpha — {lens}")
        wm = to_matrix(weekly_df, alpha_col, period_col="week")
        if wm.empty:
            st.warning("No data for this alpha lens.")
        else:
            st.plotly_chart(
                _heatmap(wm, color_label=alpha_clabel, value_fmt=alpha_fmt, unit=alpha_unit),
                use_container_width=True,
            )

        st.subheader("Weekly Total Return")
        wmr = to_matrix(weekly_df, COL_RETURN, period_col="week")
        if wmr.empty:
            st.warning("No return data.")
        else:
            st.plotly_chart(
                _heatmap(wmr, color_label="ret %", value_fmt=".1f", unit="%"),
                use_container_width=True,
            )
