"""
Execution page — Trade history and slippage monitoring.

Merges the former Trade Log and Slippage pages (Phase 3 of
dashboard-plan-optimized-260404). Trade Log tab surfaces the filterable
audit trail with outcome join and CSV download; Slippage tab surfaces
execution-quality metrics (price_at_order vs fill_price).
"""

import sys
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import list_backtest_dates, load_backtest_file, load_trades_full
from loaders.db_loader import get_score_performance


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

PAGE_SIZE = 25
BEAT_ICONS = {1: "✅", 0: "❌", True: "✅", False: "❌"}


def _beat_icon(val) -> str:
    if pd.isna(val):
        return "⏳"
    return BEAT_ICONS.get(val, "⏳")


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# Load data once (shared by both tabs)
# ---------------------------------------------------------------------------

st.title("Execution")

with st.spinner("Loading trade data..."):
    trades_df = load_trades_full()
    perf_df = get_score_performance()

if trades_df is None or trades_df.empty:
    from loaders.s3_loader import get_recent_s3_errors
    recent = get_recent_s3_errors()
    if recent:
        st.error(f"Trade data unavailable — S3 error: {recent[-1].get('error_type', '?')}: {recent[-1].get('message', '')[:100]}")
    else:
        st.warning("trades_full.csv not available yet — no trades have been executed.")
    st.stop()

trades_df = _normalize_cols(trades_df)

# Parse common columns
date_col = next((c for c in ["date", "trade_date", "timestamp"] if c in trades_df.columns), None)
if date_col:
    trades_df[date_col] = pd.to_datetime(trades_df[date_col])
    trades_df = trades_df.sort_values(date_col, ascending=False).reset_index(drop=True)

action_col = next((c for c in ["action", "signal", "trade_type"] if c in trades_df.columns), None)
ticker_col = next((c for c in ["ticker", "symbol"] if c in trades_df.columns), None)
score_col = next((c for c in ["score", "composite_score"] if c in trades_df.columns), None)
regime_col = next((c for c in ["regime", "market_regime"] if c in trades_df.columns), None)
sector_col = next((c for c in ["sector"] if c in trades_df.columns), None)
size_col = next((c for c in ["position_size", "size", "quantity", "shares"] if c in trades_df.columns), None)

# ---------------------------------------------------------------------------
# Recent activity summary (shown above tabs)
# ---------------------------------------------------------------------------

if date_col is not None and not trades_df.empty:
    latest_day = trades_df[date_col].max().date()
    latest_rows = trades_df[trades_df[date_col].dt.date == latest_day]
    r1, r2, r3, r4 = st.columns(4)
    with r1:
        st.metric("Last Session", latest_day.isoformat())
    with r2:
        st.metric("Trades That Day", f"{len(latest_rows):,}")
    with r3:
        if action_col and not latest_rows.empty:
            enters = (latest_rows[action_col].str.upper() == "ENTER").sum()
            st.metric("Entries", f"{enters}")
        else:
            st.metric("Entries", "—")
    with r4:
        if action_col and not latest_rows.empty:
            exits = latest_rows[action_col].str.upper().isin(["EXIT", "REDUCE"]).sum()
            st.metric("Exits / Reduces", f"{exits}")
        else:
            st.metric("Exits / Reduces", "—")

st.divider()

tab_log, tab_eval, tab_slip = st.tabs(["Trade Log", "Execution Evaluation", "Slippage Monitor"])

# ===========================================================================
# TAB 1: Trade Log
# ===========================================================================
with tab_log:
    st.subheader("Filters")

    f_col1, f_col2, f_col3 = st.columns([2, 2, 2])
    f_col4, f_col5 = st.columns([2, 2])

    with f_col1:
        if date_col:
            min_date = trades_df[date_col].min().date() if not trades_df.empty else date.today() - timedelta(days=365)
            max_date = trades_df[date_col].max().date() if not trades_df.empty else date.today()
            date_from = st.date_input("From Date", value=min_date, min_value=min_date, max_value=max_date)
            date_to = st.date_input("To Date", value=max_date, min_value=min_date, max_value=max_date)
        else:
            date_from = date_to = None

    with f_col2:
        if action_col:
            all_actions = sorted(trades_df[action_col].dropna().unique().tolist())
            selected_actions = st.multiselect("Action / Signal", options=all_actions, default=[])
        else:
            selected_actions = []

    with f_col3:
        ticker_filter = st.text_input("Ticker (contains)", placeholder="e.g. AAPL")

    with f_col4:
        if regime_col:
            all_regimes = sorted(trades_df[regime_col].dropna().unique().tolist())
            selected_regimes = st.multiselect("Regime", options=all_regimes, default=[])
        else:
            selected_regimes = []

    with f_col5:
        if score_col:
            trades_df[score_col] = pd.to_numeric(trades_df[score_col], errors="coerce")
            min_score_filter = st.slider("Min Score", min_value=0, max_value=100, value=0, step=5)
        else:
            min_score_filter = 0

    # ---- Apply filters ----
    filtered = trades_df.copy()

    if date_col and date_from and date_to:
        filtered = filtered[
            (filtered[date_col].dt.date >= date_from) & (filtered[date_col].dt.date <= date_to)
        ]

    if selected_actions and action_col:
        filtered = filtered[filtered[action_col].isin(selected_actions)]

    if ticker_filter and ticker_col:
        filtered = filtered[
            filtered[ticker_col].str.upper().str.contains(ticker_filter.upper(), na=False)
        ]

    if selected_regimes and regime_col:
        filtered = filtered[filtered[regime_col].isin(selected_regimes)]

    if score_col and min_score_filter > 0:
        filtered = filtered[filtered[score_col].fillna(0) >= min_score_filter]

    # ---- Outcome join for ENTER trades ----
    if perf_df is not None and not perf_df.empty and action_col and ticker_col:
        perf_df = _normalize_cols(perf_df)
        perf_ticker_col = next((c for c in ["symbol", "ticker"] if c in perf_df.columns), None)
        perf_date_col = next((c for c in ["score_date", "date"] if c in perf_df.columns), None)

        if perf_ticker_col and perf_date_col:
            perf_df[perf_date_col] = pd.to_datetime(perf_df[perf_date_col]).dt.date.astype(str)
            if date_col:
                filtered["_join_date"] = filtered[date_col].dt.date.astype(str)
            perf_subset = perf_df[[perf_ticker_col, perf_date_col, "beat_spy_10d", "beat_spy_30d"]].rename(
                columns={
                    perf_ticker_col: ticker_col,
                    perf_date_col: "_join_date",
                }
            )
            if "_join_date" in filtered.columns:
                enter_mask = filtered[action_col].str.upper() == "ENTER"
                enter_rows = filtered[enter_mask].merge(
                    perf_subset, on=[ticker_col, "_join_date"], how="left"
                )
                non_enter_rows = filtered[~enter_mask].copy()
                non_enter_rows["beat_spy_10d"] = None
                non_enter_rows["beat_spy_30d"] = None
                filtered = pd.concat([enter_rows, non_enter_rows], ignore_index=True)

                if date_col in filtered.columns:
                    filtered = filtered.sort_values(date_col, ascending=False)

    st.caption(f"Showing {len(filtered):,} of {len(trades_df):,} trades")

    # ---- Trade Table with Pagination ----
    st.subheader("Trade History")

    display_filtered = filtered.copy()
    for col in ["beat_spy_10d", "beat_spy_30d"]:
        if col in display_filtered.columns:
            display_filtered[col] = display_filtered[col].apply(_beat_icon)
    if "_join_date" in display_filtered.columns:
        display_filtered = display_filtered.drop(columns=["_join_date"])

    total_pages = max(1, (len(display_filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page_num = st.number_input(
        f"Page (1–{total_pages})",
        min_value=1,
        max_value=total_pages,
        value=1,
        step=1,
    )
    start = (page_num - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_df = display_filtered.iloc[start:end]

    st.dataframe(page_df, use_container_width=True, hide_index=True)

    # ---- Download Button ----
    csv_data = filtered.drop(columns=["_join_date"], errors="ignore").to_csv(index=False)
    st.download_button(
        label="Download Filtered Trades (CSV)",
        data=csv_data,
        file_name="trades_filtered.csv",
        mime="text/csv",
    )

    st.divider()

    # ---- Trade Summary Stats ----
    st.subheader("Trade Summary Stats")

    s_col1, s_col2, s_col3, s_col4 = st.columns(4)

    with s_col1:
        st.metric("Total Trades", f"{len(filtered):,}")
        if action_col:
            action_counts = filtered[action_col].value_counts()
            action_summary = ", ".join([f"{k}: {v}" for k, v in action_counts.items()])
            st.caption(f"By action: {action_summary}")

    with s_col2:
        if score_col:
            avg_score = pd.to_numeric(filtered[score_col], errors="coerce").mean()
            st.metric("Avg Score", f"{avg_score:.1f}" if pd.notna(avg_score) else "—")
        else:
            st.metric("Avg Score", "—")

    with s_col3:
        if regime_col:
            most_common_regime = filtered[regime_col].value_counts().idxmax() if not filtered[regime_col].empty else "—"
            st.metric("Most Common Regime", str(most_common_regime))
        else:
            st.metric("Most Common Regime", "—")

    with s_col4:
        if size_col:
            avg_size = pd.to_numeric(filtered[size_col], errors="coerce").mean()
            st.metric("Avg Position Size", f"{avg_size:.2f}" if pd.notna(avg_size) else "—")
        else:
            st.metric("Avg Position Size", "—")

    if sector_col and not filtered.empty:
        st.subheader("Most Active Sectors")
        sector_counts = filtered[sector_col].value_counts().head(5).reset_index()
        sector_counts.columns = ["Sector", "Trade Count"]
        st.dataframe(sector_counts, use_container_width=True, hide_index=True)


# ===========================================================================
# TAB 2: Execution Evaluation (backtester analysis)
# ===========================================================================
with tab_eval:
    bt_dates = list_backtest_dates()
    if not bt_dates:
        st.info("No backtest results available. Run the backtester to populate execution evaluation.")
    else:
        bt_date = bt_dates[0]
        with st.spinner(f"Loading evaluation data from {bt_date}..."):
            trigger_data = load_backtest_file(bt_date, "trigger_scorecard.json")
            shadow_data = load_backtest_file(bt_date, "shadow_book.json")
            exit_data = load_backtest_file(bt_date, "exit_timing.json")

        st.caption(f"Source: backtest run {bt_date}")

        # ---- Entry Trigger Scorecard ----
        st.subheader("Entry Trigger Scorecard")
        if trigger_data and trigger_data.get("status") == "ok":
            triggers = trigger_data.get("triggers", [])
            if triggers:
                tdf = pd.DataFrame(triggers)
                display_cols = [c for c in ["trigger", "n_trades", "avg_slippage_vs_signal", "avg_slippage_vs_open", "avg_realized_alpha", "win_rate_vs_spy", "precision"] if c in tdf.columns]
                st.dataframe(tdf[display_cols], use_container_width=True, hide_index=True)

                summary = trigger_data.get("summary", {})
                c1, c2, c3 = st.columns(3)
                with c1:
                    v = summary.get("avg_slippage_vs_signal")
                    st.metric("Avg Slippage vs Signal", f"{v:+.2%}" if v is not None else "—")
                with c2:
                    v = summary.get("win_rate_vs_spy")
                    st.metric("Win Rate vs SPY", f"{v:.1%}" if v is not None else "—")
                with c3:
                    v = summary.get("avg_realized_alpha")
                    st.metric("Avg Realized Alpha", f"{v:+.2%}" if v is not None else "—")
            else:
                st.info("No trigger data with enough trades.")
        else:
            st.info("Trigger scorecard not available for this backtest run.")

        st.divider()

        # ---- Risk Guard Shadow Book ----
        st.subheader("Risk Guard Shadow Book")
        if shadow_data and shadow_data.get("status") == "ok":
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Blocked Entries", shadow_data.get("n_blocked", 0))
            with c2:
                gl = shadow_data.get("guard_lift")
                st.metric("Guard Lift", f"{gl:+.2%}" if gl is not None else "—")
            with c3:
                st.metric("Assessment", shadow_data.get("assessment", "—").replace("_", " ").title())

            clf = shadow_data.get("classification")
            if clf:
                mc1, mc2, mc3 = st.columns(3)
                with mc1:
                    v = clf.get("precision")
                    st.metric("Precision", f"{v:.1%}" if v is not None else "—", help="% of blocks that were actual losers")
                with mc2:
                    v = clf.get("recall")
                    st.metric("Recall", f"{v:.1%}" if v is not None else "—", help="% of all losers that were blocked")
                with mc3:
                    v = clf.get("f1")
                    st.metric("F1", f"{v:.3f}" if v is not None else "—")

            by_reason = shadow_data.get("by_reason", [])
            if by_reason:
                st.markdown("**Blocks by Reason**")
                st.dataframe(pd.DataFrame(by_reason), use_container_width=True, hide_index=True)
        else:
            st.info("Shadow book analysis not available for this backtest run.")

        st.divider()

        # ---- Exit Timing Analysis ----
        st.subheader("Exit Timing (MFE/MAE)")
        if exit_data and exit_data.get("status") == "ok":
            summary = exit_data.get("summary", {})
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                v = summary.get("avg_capture_ratio")
                st.metric("Capture Ratio", f"{v:.2f}" if v is not None else "—", help="Realized return / max favorable excursion")
            with c2:
                v = summary.get("avg_mfe")
                st.metric("Avg MFE", f"{v:+.2%}" if v is not None else "—")
            with c3:
                v = summary.get("avg_mae")
                st.metric("Avg MAE", f"{v:+.2%}" if v is not None else "—")
            with c4:
                st.metric("Diagnosis", exit_data.get("diagnosis", "—").replace("_", " ").title())

            by_exit = exit_data.get("by_exit_type", [])
            if by_exit:
                st.markdown("**By Exit Type**")
                st.dataframe(pd.DataFrame(by_exit), use_container_width=True, hide_index=True)

            st.metric("Roundtrips", exit_data.get("n_roundtrips", 0))
        else:
            st.info("Exit timing analysis not available for this backtest run.")


# ===========================================================================
# TAB 3: Slippage Monitor
# ===========================================================================
with tab_slip:
    if "fill_price" not in trades_df.columns or "price_at_order" not in trades_df.columns:
        st.info(
            "Slippage data not yet available. The executor needs to run with "
            "fill confirmation enabled (deployed 2026-03-17) before slippage "
            "metrics can be computed."
        )
        st.stop()

    slip_src = trades_df.copy()
    slip_src["fill_price"] = pd.to_numeric(slip_src["fill_price"], errors="coerce")
    slip_src["price_at_order"] = pd.to_numeric(slip_src["price_at_order"], errors="coerce")

    has_both = slip_src["fill_price"].notna() & slip_src["price_at_order"].notna()
    slippage_df = slip_src[has_both].copy()

    if slippage_df.empty:
        st.info("No trades with fill price data yet. Slippage metrics will appear after the next live trading session.")
        st.stop()

    slippage_df["slippage_bps"] = (
        (slippage_df["fill_price"] - slippage_df["price_at_order"])
        / slippage_df["price_at_order"]
        * 10_000
    ).round(2)

    # Normalize: positive = unfavorable for all actions
    if action_col:
        sell_mask = slippage_df[action_col].str.upper().isin(["SELL", "EXIT", "REDUCE"])
        slippage_df.loc[sell_mask, "slippage_bps"] = -slippage_df.loc[sell_mask, "slippage_bps"]

    # ---- Summary metrics ----
    st.subheader("Summary")

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("Trades with Fill Data", f"{len(slippage_df):,}")
    with m2:
        mean_slip = slippage_df["slippage_bps"].mean()
        st.metric("Mean Slippage", f"{mean_slip:+.1f} bps")
    with m3:
        median_slip = slippage_df["slippage_bps"].median()
        st.metric("Median Slippage", f"{median_slip:+.1f} bps")
    with m4:
        p95_slip = slippage_df["slippage_bps"].quantile(0.95)
        st.metric("P95 Slippage", f"{p95_slip:+.1f} bps")
    with m5:
        pct_negative = (slippage_df["slippage_bps"] > 0).mean() * 100
        st.metric("% Unfavorable", f"{pct_negative:.0f}%")

    # ---- Distribution ----
    st.subheader("Slippage Distribution (bps)")
    fig_hist = px.histogram(
        slippage_df,
        x="slippage_bps",
        nbins=50,
        title="Slippage Distribution (positive = unfavorable)",
        labels={"slippage_bps": "Slippage (bps)"},
        color_discrete_sequence=["#1f77b4"],
    )
    fig_hist.add_vline(x=0, line_dash="dash", line_color="red", annotation_text="Zero")
    fig_hist.update_layout(height=350)
    st.plotly_chart(fig_hist, use_container_width=True)

    # ---- By action ----
    if action_col:
        st.subheader("Slippage by Action")
        action_stats = (
            slippage_df.groupby(action_col)["slippage_bps"]
            .agg(["mean", "median", "std", "count"])
            .round(2)
            .reset_index()
        )
        action_stats.columns = ["Action", "Mean (bps)", "Median (bps)", "Std Dev", "Count"]
        st.dataframe(action_stats, use_container_width=True, hide_index=True)

    # ---- By regime ----
    if regime_col:
        st.subheader("Slippage by Market Regime")
        regime_stats = (
            slippage_df.groupby(regime_col)["slippage_bps"]
            .agg(["mean", "median", "count"])
            .round(2)
            .reset_index()
        )
        regime_stats.columns = ["Regime", "Mean (bps)", "Median (bps)", "Count"]
        st.dataframe(regime_stats, use_container_width=True, hide_index=True)

    # ---- Over time ----
    if date_col:
        st.subheader("Slippage Over Time")
        daily_slip = (
            slippage_df.groupby(slippage_df[date_col].dt.date)["slippage_bps"]
            .mean()
            .reset_index()
        )
        daily_slip.columns = ["Date", "Mean Slippage (bps)"]
        fig_time = px.line(
            daily_slip,
            x="Date",
            y="Mean Slippage (bps)",
            title="Daily Mean Slippage",
        )
        fig_time.add_hline(y=0, line_dash="dash", line_color="gray")
        fig_time.update_layout(height=350)
        st.plotly_chart(fig_time, use_container_width=True)

    # ---- Worst events ----
    st.subheader("Worst Slippage Events")
    display_cols = [
        c for c in [date_col, ticker_col, action_col, "price_at_order", "fill_price", "slippage_bps", "shares"]
        if c and c in slippage_df.columns
    ]
    worst = slippage_df.nlargest(20, "slippage_bps")[display_cols]
    st.dataframe(worst, use_container_width=True, hide_index=True)
