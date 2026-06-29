"""
Portfolio page — NAV chart, drawdown, positions, sector allocation, P&L, summary stats.
"""

import json
import logging
import sys
import os
from datetime import date

logger = logging.getLogger(__name__)

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import load_config, load_eod_pnl, load_trades_full, load_signals_json
from loaders.signal_loader import signals_to_df
from loaders.utils import safe_column
from charts.nav_chart import make_nav_chart
from charts.alpha_chart import make_alpha_chart
from charts.portfolio_chart import make_sector_allocation_chart, make_sector_rotation_chart
from shared.formatters import format_pct, format_dollar, color_return
from shared.normalizers import to_decimal_series
from shared.accuracy_metrics import compute_drawdown, compute_sharpe, find_drawdown_episodes
from shared.constants import get_thresholds
from shared.position_pnl import (
    compute_position_lifecycles,
    enrich_positions,
    parse_positions_snapshot,
)
from shared.attribution import build_long_frame
from shared.correlation import (
    correlation_matrix,
    high_correlation_pairs,
    holdings_return_matrix,
)

_TH = get_thresholds()
_HHI_DIVERSIFIED = _TH["hhi_diversified"]
_HHI_CONCENTRATED = _TH["hhi_concentrated"]
_SHARPE_MIN_ROWS = int(_TH["sharpe_min_rows"])
_CORR_HIGH = float(_TH["correlation_high"])
_CORR_MIN_OVERLAP = int(_TH["correlation_min_overlap"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Aliases for backward compatibility within this file
_to_decimal = to_decimal_series
_compute_drawdown = compute_drawdown
_compute_sharpe = compute_sharpe


_find_drawdown_episodes = find_drawdown_episodes


from shared.constants import DEFAULT_CACHE_TTL_SECONDS


def _parse_snapshot_row(row_date: str, snapshot_json: str) -> list[dict]:
    """Parse a single positions_snapshot JSON string into flat sector/value records.

    Returns an empty list if parsing fails.
    """
    try:
        positions = json.loads(str(snapshot_json))
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug("Skipping unparseable snapshot: %s", e)
        return []

    if isinstance(positions, dict):
        positions = [positions]
    if not isinstance(positions, list):
        return []

    records = []
    for pos in positions:
        try:
            market_value = float(pos.get("market_value", 0) or 0)
        except (ValueError, TypeError):
            market_value = 0.0
        records.append({
            "date": row_date,
            "sector": pos.get("sector", "Unknown"),
            "market_value": market_value,
        })
    return records


@st.cache_data(ttl=DEFAULT_CACHE_TTL_SECONDS)
def _parse_all_snapshots(eod_csv_bytes: bytes) -> list[dict]:
    """Parse positions_snapshot JSON from every eod_pnl row into flat records."""
    eod_df = pd.read_csv(pd.io.common.BytesIO(eod_csv_bytes))
    if "positions_snapshot" not in eod_df.columns or "date" not in eod_df.columns:
        return []

    records: list[dict] = []
    for _, row in eod_df.iterrows():
        snap_raw = row.get("positions_snapshot")
        if pd.isna(snap_raw) or not snap_raw:
            continue
        records.extend(_parse_snapshot_row(row["date"], snap_raw))
    return records


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

st.title("Portfolio Overview")
st.caption(f"Last updated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} UTC")

cfg = load_config()
circuit_breaker = cfg.get("drawdown_circuit_breaker", -0.08)
max_sector_pct = cfg.get("risk_limits", {}).get("max_sector_pct", 0.25)

# Load data
with st.spinner("Loading portfolio data..."):
    eod_df = load_eod_pnl()
    trades_df = load_trades_full()
    today = date.today().isoformat()
    signals_data = load_signals_json(today)

if eod_df is None or eod_df.empty:
    from loaders.s3_loader import get_recent_s3_errors
    recent = get_recent_s3_errors()
    if recent:
        st.error(f"Portfolio data unavailable — last S3 error: {recent[-1].get('error_type', '?')}: {recent[-1].get('message', '')[:100]}")
    else:
        st.warning("Portfolio data (eod_pnl.csv) not available yet — EOD reconciliation may not have run.")
    st.stop()

eod_df["date"] = pd.to_datetime(eod_df["date"])
eod_df = eod_df.sort_values("date").reset_index(drop=True)

daily_ret = _to_decimal(eod_df["daily_return_pct"])
spy_ret = _to_decimal(eod_df["spy_return_pct"])

# ---------------------------------------------------------------------------
# Section 1: NAV vs SPY
# ---------------------------------------------------------------------------
st.header("NAV vs SPY — Cumulative Return")
nav_fig = make_nav_chart(eod_df)
st.plotly_chart(nav_fig, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 2: Daily Alpha
# ---------------------------------------------------------------------------
st.header("Daily Alpha")
alpha_fig = make_alpha_chart(eod_df)
st.plotly_chart(alpha_fig, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 3: Drawdown
# ---------------------------------------------------------------------------
st.header("Drawdown")

drawdown = _compute_drawdown(daily_ret)
drawdown_pct = drawdown * 100

drawdown_fig = go.Figure()

drawdown_fig.add_trace(
    go.Scatter(
        x=eod_df["date"],
        y=drawdown_pct,
        fill="tozeroy",
        mode="lines",
        fillcolor="rgba(214,39,40,0.25)",
        line=dict(color="#d62728", width=1.5),
        name="Drawdown",
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Drawdown: %{y:.2f}%<extra></extra>",
    )
)

# Circuit breaker line
drawdown_fig.add_hline(
    y=circuit_breaker * 100,
    line=dict(color="#ff7f0e", width=2, dash="dash"),
    annotation_text=f"Circuit Breaker ({circuit_breaker * 100:.0f}%)",
    annotation_position="top right",
    annotation_font_color="#ff7f0e",
)

drawdown_fig.update_layout(
    xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
    yaxis=dict(
        title="Drawdown (%)",
        ticksuffix="%",
        showgrid=True,
        gridcolor="rgba(0,0,0,0.07)",
        zeroline=True,
        zerolinecolor="rgba(0,0,0,0.3)",
    ),
    plot_bgcolor="white",
    paper_bgcolor="white",
    margin=dict(t=20, b=40, l=60, r=20),
    showlegend=False,
)

# Circuit breaker breach alert
max_dd = drawdown_pct.min()
if max_dd <= circuit_breaker * 100:
    st.error(
        f"Circuit breaker breached! Max drawdown: {max_dd:.2f}% "
        f"(threshold: {circuit_breaker * 100:.0f}%)"
    )

st.plotly_chart(drawdown_fig, use_container_width=True)

# --- Drawdown Recovery Episodes (Gap #9) ---
episodes = _find_drawdown_episodes(drawdown, eod_df["date"])
if episodes:
    st.subheader("Drawdown Episodes")

    recovered = [e for e in episodes if e["Status"] == "Recovered"]
    if recovered:
        recovery_days = [e["Days to Recovery"] for e in recovered]
        avg_recovery = sum(recovery_days) / len(recovery_days)
        max_recovery = max(recovery_days)
        ep_col1, ep_col2, ep_col3 = st.columns(3)
        with ep_col1:
            st.metric("Avg Recovery Time", f"{avg_recovery:.0f} days")
        with ep_col2:
            st.metric("Longest Recovery", f"{max_recovery} days")
        with ep_col3:
            st.metric("Total Episodes", str(len(episodes)))

    ep_df = pd.DataFrame(episodes)
    st.dataframe(ep_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Section 4: Current Positions
# ---------------------------------------------------------------------------
st.header("Current Positions")

positions_df = parse_positions_snapshot(eod_df)

if positions_df is not None and not positions_df.empty:
    sig_df = signals_to_df(signals_data) if signals_data else None
    positions_df = enrich_positions(positions_df, sig_df, trades_df)

    # P&L summary metrics
    if "unrealized_pnl" in positions_df.columns:
        total_pnl = positions_df["unrealized_pnl"].sum()
        pos_count = len(positions_df)
        avg_days = positions_df["days_held"].mean() if "days_held" in positions_df.columns else None
        best_ret = positions_df["return_pct"].max() if "return_pct" in positions_df.columns else None
        worst_ret = positions_df["return_pct"].min() if "return_pct" in positions_df.columns else None

        pnl_c1, pnl_c2, pnl_c3, pnl_c4 = st.columns(4)
        with pnl_c1:
            color = "normal" if total_pnl >= 0 else "inverse"
            st.metric("Total Unrealized P&L", format_dollar(total_pnl))
        with pnl_c2:
            st.metric("Positions", str(pos_count))
        with pnl_c3:
            st.metric("Avg Days Held", f"{avg_days:.0f}" if avg_days is not None else "—")
        with pnl_c4:
            if best_ret is not None and worst_ret is not None:
                st.metric("Best / Worst", f"{best_ret*100:+.1f}% / {worst_ret*100:+.1f}%")
            else:
                st.metric("Best / Worst", "—")

    # Display columns
    display_cols = [
        c for c in [
            "ticker", "sector", "shares", "entry_price", "current_price",
            "unrealized_pnl", "return_pct", "days_held", "score", "signal",
        ]
        if c in positions_df.columns
    ]

    if display_cols:
        display_pos = positions_df[display_cols].copy()

        styled = display_pos.style
        if "return_pct" in display_pos.columns:
            styled = styled.map(color_return, subset=["return_pct"])
            styled = styled.format({"return_pct": "{:.1%}"}, na_rep="—")
        if "unrealized_pnl" in display_pos.columns:
            styled = styled.format({"unrealized_pnl": "${:,.2f}"}, na_rep="—")
        if "entry_price" in display_pos.columns:
            styled = styled.format({"entry_price": "${:.2f}"}, na_rep="—")
        if "current_price" in display_pos.columns:
            styled = styled.format({"current_price": "${:.2f}"}, na_rep="—")
        if "score" in display_pos.columns:
            styled = styled.format({"score": "{:.1f}"}, na_rep="—")

        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.dataframe(positions_df, use_container_width=True, hide_index=True)

    # --- Sector Allocation (Gap #1 + Gap #3: HHI) ---
    if "sector" in positions_df.columns and "market_value" in positions_df.columns:
        st.header("Sector Allocation")

        col_chart, col_table = st.columns([2, 1])

        with col_chart:
            sector_fig = make_sector_allocation_chart(positions_df)
            st.plotly_chart(sector_fig, use_container_width=True)

        with col_table:
            mv = pd.to_numeric(positions_df["market_value"], errors="coerce").fillna(0)
            sector_summary = positions_df.assign(market_value=mv).groupby("sector").agg(
                Count=("ticker", "count"),
                Value=("market_value", "sum"),
            ).reset_index()
            total_val = sector_summary["Value"].sum()
            sector_summary["Weight"] = sector_summary["Value"] / total_val if total_val > 0 else 0
            sector_summary["Limit"] = sector_summary["Weight"].apply(
                lambda w: "LIMIT" if w > max_sector_pct else ""
            )
            sector_summary["Value"] = sector_summary["Value"].apply(lambda v: f"${v:,.0f}")
            sector_summary["Weight"] = sector_summary["Weight"].apply(lambda w: f"{w:.1%}")
            st.dataframe(sector_summary, use_container_width=True, hide_index=True)

            # HHI concentration metric
            weights = mv.groupby(positions_df["sector"]).sum()
            if total_val > 0:
                weight_pcts = weights / total_val
                hhi = (weight_pcts ** 2).sum()
                if hhi < _HHI_DIVERSIFIED:
                    hhi_label = "Diversified"
                    hhi_color = "green"
                elif hhi < _HHI_CONCENTRATED:
                    hhi_label = "Moderate"
                    hhi_color = "orange"
                else:
                    hhi_label = "Concentrated"
                    hhi_color = "red"
                st.metric("HHI Concentration", f"{hhi:.3f} ({hhi_label})")

        # --- Holdings Correlation (config#953) ---
        # The HHI metric above is the static concentration half; this is the
        # correlation half, previously stubbed as "requires price history
        # integration". It is no longer blocked: build_long_frame exposes a
        # per-holding daily-return series (stored post-2026-04-20, reconstructed
        # from closing_price before that), so we can correlate the names actually
        # held and surface the MSFT+AAPL+GOOGL>0.8 clustering the issue asks for.
        st.subheader("Holdings Correlation")
        held_tickers = (
            positions_df["ticker"].dropna().astype(str).unique().tolist()
            if "ticker" in positions_df.columns
            else []
        )
        corr = correlation_matrix(
            holdings_return_matrix(build_long_frame(eod_df), held_tickers),
            min_overlap=_CORR_MIN_OVERLAP,
        )
        if corr.empty:
            st.info(
                "Not enough overlapping daily-return history to correlate current "
                f"holdings yet (need ≥{_CORR_MIN_OVERLAP} shared trading days per "
                "pair). The matrix appears as positions accumulate history."
            )
        else:
            corr_fig = go.Figure(
                data=go.Heatmap(
                    z=corr.values,
                    x=list(corr.columns),
                    y=list(corr.index),
                    zmin=-1.0,
                    zmax=1.0,
                    colorscale="RdBu_r",
                    colorbar=dict(title="ρ"),
                    hovertemplate="%{y} ↔ %{x}: %{z:.2f}<extra></extra>",
                )
            )
            corr_fig.update_layout(
                height=max(320, 34 * len(corr.columns)),
                margin=dict(l=10, r=10, t=30, b=10),
                yaxis=dict(autorange="reversed"),
            )
            st.plotly_chart(corr_fig, use_container_width=True)

            pairs = high_correlation_pairs(corr, threshold=_CORR_HIGH)
            if pairs:
                pairs_df = pd.DataFrame(
                    [(a, b, f"{c:+.2f}") for a, b, c in pairs],
                    columns=["Holding A", "Holding B", "Correlation"],
                )
                st.caption(
                    f"Highly correlated pairs (|ρ| ≥ {_CORR_HIGH:g}) — concentrated "
                    "exposure / limited diversification between these names:"
                )
                st.dataframe(pairs_df, use_container_width=True, hide_index=True)
            else:
                st.caption(
                    f"No holding pairs exceed |ρ| ≥ {_CORR_HIGH:g} over the shared "
                    "return window — pairwise diversification looks healthy."
                )

    # --- Sector Rotation Over Time (Gap #8) ---
    if "positions_snapshot" in eod_df.columns:
        st.header("Sector Rotation")

        # Build CSV bytes for caching
        try:
            csv_buf = eod_df.to_csv(index=False).encode("utf-8")
            snapshot_records = _parse_all_snapshots(csv_buf)

            if snapshot_records:
                time_range = st.radio(
                    "Time range", ["30d", "90d", "all"], horizontal=True, index=2,
                    key="sector_rotation_range"
                )
                rotation_fig = make_sector_rotation_chart(snapshot_records, time_range)
                st.plotly_chart(rotation_fig, use_container_width=True)
            else:
                st.info("No position snapshots available for sector rotation chart.")
        except Exception as e:
            logger.warning("Sector rotation chart failed: %s", e)
            st.info("Could not parse position snapshots for rotation chart.")

else:
    st.info("No positions snapshot available in today's data.")

# ---------------------------------------------------------------------------
# Section 4b: Position Lifecycle History (ROADMAP L137)
# ---------------------------------------------------------------------------
#
# Per-trade `realized_pnl` (single fill) lives in trades.db; daily
# portfolio NAV decomposition lives in eod_pnl.csv. Neither rolls up to
# per-position-lifecycle ("AAPL opened 4/15, closed 5/08, total P&L
# $X"). This section is that rollup — closes the L137 gap PR #100 had
# to reframe to "per-trade realized P&L."

st.header("Position Lifecycle History")
st.caption(
    "One row per opened position. Entry trade + linked exits "
    "(REDUCE / EXIT / COVER) collapsed via `entry_trade_id`. "
    "Status: **closed** (all shares exited), **open_partial** "
    "(REDUCE only), **open** (no linked exits). Closed ROADMAP L137."
)

lifecycles_df = compute_position_lifecycles(trades_df)

if lifecycles_df is None or lifecycles_df.empty:
    st.info(
        "No position lifecycles to roll up yet. Surface populates once "
        "`trades.db` has at least one ENTER record. Each entry's linked "
        "exits collapse on the entry's `trade_id` via the `entry_trade_id` "
        "column (alpha-engine `executor/trade_logger.py:57`)."
    )
else:
    closed = lifecycles_df[lifecycles_df["status"] == "closed"]
    open_partial = lifecycles_df[lifecycles_df["status"] == "open_partial"]
    open_full = lifecycles_df[lifecycles_df["status"] == "open"]

    lcols = st.columns(5)
    lcols[0].metric("Closed", len(closed))
    lcols[1].metric("Open (partial)", len(open_partial))
    lcols[2].metric("Open", len(open_full))
    if not closed.empty:
        total_realized = float(closed["total_realized_pnl"].sum())
        lcols[3].metric(
            "Realized P&L (closed)",
            f"${total_realized:,.0f}",
            delta=f"{(total_realized / max(1, len(closed))):,.0f} avg",
        )
        median_hold = closed["holding_days"].median()
        if pd.notna(median_hold):
            lcols[4].metric("Median holding (days)", f"{int(median_hold)}")

    # Render the table. Closed positions first (most operationally
    # interesting — the realized-attribution view); open last.
    display_cols = [
        c for c in (
            "ticker", "sector", "entry_date", "exit_date", "holding_days",
            "entry_price", "shares_entered", "n_exits",
            "total_realized_pnl", "total_realized_return_pct",
            "total_realized_alpha_pct", "status",
        ) if c in lifecycles_df.columns
    ]
    display_df = lifecycles_df[display_cols].copy()
    for date_col in ("entry_date", "exit_date"):
        if date_col in display_df.columns:
            display_df[date_col] = pd.to_datetime(
                display_df[date_col], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
    if "total_realized_pnl" in display_df.columns:
        display_df["total_realized_pnl"] = display_df["total_realized_pnl"].map(
            lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
        )
    for pct_col in ("total_realized_return_pct", "total_realized_alpha_pct"):
        if pct_col in display_df.columns:
            display_df[pct_col] = display_df[pct_col].map(
                lambda v: f"{v * 100:.1f}%" if pd.notna(v) and isinstance(v, (int, float)) else "—"
            )
    st.dataframe(display_df, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 5: Portfolio Summary Stats
# ---------------------------------------------------------------------------
st.header("Portfolio Summary Stats")

total_return = ((1 + daily_ret).prod() - 1)
sharpe = _compute_sharpe(daily_ret, min_rows=_SHARPE_MIN_ROWS)
max_drawdown = drawdown.min()
best_day = daily_ret.max()
worst_day = daily_ret.min()
days_positive = int((daily_ret > 0).sum())
days_negative = int((daily_ret < 0).sum())
alpha_series = _to_decimal(eod_df["daily_alpha_pct"]) if "daily_alpha_pct" in eod_df.columns else pd.Series(dtype=float)
avg_daily_alpha = alpha_series.mean() if not alpha_series.empty else None

stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
stat_col5, stat_col6, stat_col7, stat_col8 = st.columns(4)

with stat_col1:
    st.metric("Total Return", format_pct(total_return))

with stat_col2:
    if sharpe is not None:
        st.metric("Sharpe Ratio", f"{sharpe:.2f}")
    else:
        st.metric("Sharpe Ratio", f"Need ≥{_SHARPE_MIN_ROWS} days ({len(daily_ret)} available)")

with stat_col3:
    st.metric("Max Drawdown", format_pct(max_drawdown))

with stat_col4:
    st.metric("Best Day", format_pct(best_day))

with stat_col5:
    st.metric("Worst Day", format_pct(worst_day))

with stat_col6:
    st.metric("Days Positive", f"{days_positive}")

with stat_col7:
    st.metric("Days Negative", f"{days_negative}")

with stat_col8:
    if avg_daily_alpha is not None:
        st.metric("Avg Daily Alpha", format_pct(avg_daily_alpha))
    else:
        st.metric("Avg Daily Alpha", "—")
