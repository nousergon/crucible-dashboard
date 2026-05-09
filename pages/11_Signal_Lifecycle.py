"""
Alpha Engine — Signal Lifecycle (private console)

Walks one ticker through the full pipeline on a single screen — research
thesis → composite score → predictor verdict → veto gate → position
sizing → entry execution → EOD attribution → backtester accuracy.

Designed as the *centerpiece of the interview demo* (per plan §3.2):
one screen tells the entire system story end-to-end. Existing
Signals & Research and Execution pages handle slices of this; this page
is the unified narrative for one chosen ticker.

Lives on console.nousergon.ai (Cloudflare Access-gated). Every datum
sources from existing system outputs (Decision 11) — research.db,
signals.json, predictions.json, order_book summary, trades_full,
eod_pnl, score_performance.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from components.header import render_footer, render_header
from components.styles import inject_base_css, inject_docs_css
from loaders.db_loader import (
    get_investment_thesis,
    get_predictor_outcomes,
    get_score_history,
    get_score_performance,
)
from loaders.s3_loader import (
    load_eod_pnl,
    load_order_book_summary,
    load_predictions_json,
    load_signals_json,
    load_trades_full,
    predictor_horizon_days,
    predictor_label_domain,
)
from loaders.signal_loader import get_available_signal_dates, load_signals, signals_to_df

st.set_page_config(
    page_title="Signal Lifecycle — Alpha Engine",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_base_css()
inject_docs_css()
render_header(current_page="Signal Lifecycle")

st.divider()

# ---------------------------------------------------------------------------
# Page intro + ticker/date selector
# ---------------------------------------------------------------------------

st.markdown("### Signal Lifecycle")
st.markdown(
    "Walk one ticker through the full pipeline — research thesis → "
    "composite score → predictor verdict → veto gate → position sizing → "
    "entry execution → EOD attribution → backtester accuracy. One screen, "
    "end-to-end."
)

available_dates = get_available_signal_dates() or []

# URL params for deep-linking (?date=2026-05-02&ticker=AAPL)
qp = st.query_params
default_date = qp.get("date") if qp.get("date") in available_dates else (available_dates[0] if available_dates else None)

col_date, col_ticker = st.columns([1, 2])

with col_date:
    if not available_dates:
        st.warning("No signal dates available yet.")
        st.stop()
    selected_date = st.selectbox(
        "Signal date",
        available_dates,
        index=available_dates.index(default_date) if default_date else 0,
    )

# Load signals for the selected date to populate the ticker dropdown
signals_data = load_signals(selected_date)
signals_df = signals_to_df(signals_data) if signals_data else pd.DataFrame()

with col_ticker:
    if signals_df.empty:
        st.warning(f"No signals found for {selected_date}.")
        st.stop()
    tickers = sorted(signals_df["ticker"].dropna().unique().tolist())
    default_ticker = qp.get("ticker") if qp.get("ticker") in tickers else tickers[0]
    selected_ticker = st.selectbox(
        "Ticker",
        tickers,
        index=tickers.index(default_ticker) if default_ticker in tickers else 0,
    )

# Update URL params
st.query_params["date"] = selected_date
st.query_params["ticker"] = selected_ticker

ticker_row = signals_df[signals_df["ticker"] == selected_ticker].iloc[0]

st.divider()

# ---------------------------------------------------------------------------
# Stage 1 — Header card
# ---------------------------------------------------------------------------

_signal = str(ticker_row.get("signal", "—")).upper()
_signal_color = {
    "BUY": "#7fd17f",
    "HOLD": "#e0c050",
    "SELL": "#d06060",
    "ENTER": "#7fd17f",
    "EXIT": "#d06060",
}.get(_signal, "#888")
_composite = ticker_row.get("composite_score")
_sector = ticker_row.get("sector") or "—"

st.markdown(
    f"""
    <div style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
                border-radius: 10px; padding: 22px 26px; margin-bottom: 14px;">
        <div style="display:flex; align-items:center; gap:24px;">
            <div style="font-size:34px; font-weight:700;">{selected_ticker}</div>
            <div style="font-size:13px; color:#aaa;">Sector: {_sector}</div>
            <div style="font-size:13px; color:#aaa;">Signal date: {selected_date}</div>
            <div style="margin-left:auto; padding:6px 14px; border-radius:6px;
                        background:{_signal_color}; color:#000; font-weight:600;">
                {_signal}
            </div>
            <div style="font-size:24px; font-weight:600; color:#1a73e8;">
                {f"{_composite:.1f}" if pd.notna(_composite) else "—"}
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Stage 2 — Research thesis
# ---------------------------------------------------------------------------

st.markdown("## 1. Research thesis")
st.caption(
    "Source: `research.db:investment_thesis` (producer: Research Lambda "
    "via `archive/manager.py`). Most recent entry for this ticker."
)

thesis_df = get_investment_thesis(symbol=selected_ticker)
if thesis_df is not None and not thesis_df.empty:
    latest_thesis = thesis_df.iloc[0]
    thesis_date = latest_thesis.get("date", "—")
    bull = latest_thesis.get("bull_case") or latest_thesis.get("thesis_summary") or ""
    bear = latest_thesis.get("bear_case") or ""
    catalyst = latest_thesis.get("catalyst") or ""
    conviction = latest_thesis.get("conviction") or "—"
    target = latest_thesis.get("price_target") or "—"

    cmeta1, cmeta2, cmeta3 = st.columns(3)
    cmeta1.metric("Thesis date", str(thesis_date))
    cmeta2.metric("Conviction", str(conviction))
    cmeta3.metric("Price target", f"${target}" if isinstance(target, (int, float)) else str(target))

    with st.expander("Bull case", expanded=True):
        st.markdown(bull or "_(not provided)_")
    if bear:
        with st.expander("Bear case"):
            st.markdown(bear)
    if catalyst:
        with st.expander("Catalyst"):
            st.markdown(catalyst)
else:
    st.info(f"No thesis archived for {selected_ticker}.")

st.divider()

# ---------------------------------------------------------------------------
# Stage 3 — Composite score breakdown
# ---------------------------------------------------------------------------

st.markdown("## 2. Composite score breakdown")
st.caption(
    "Source: `signals/{date}/signals.json` (producer: Research scoring "
    "pipeline via `scoring/composite.py`). Sub-scores combine into the "
    "composite via configurable weights (auto-tuned by the backtester)."
)

sub_score_keys = ["technical", "news", "research", "qual", "quant"]
sub_scores = {k: ticker_row.get(k) for k in sub_score_keys if k in ticker_row.index and pd.notna(ticker_row.get(k))}
macro_modifier = ticker_row.get("sector_macro_modifier") or ticker_row.get("macro_modifier")

if sub_scores:
    bar_fig = go.Figure(go.Bar(
        x=list(sub_scores.keys()),
        y=[float(v) for v in sub_scores.values()],
        marker_color="#1a73e8",
        hovertemplate="<b>%{x}</b>: %{y:.1f}<extra></extra>",
    ))
    bar_fig.add_hline(y=float(_composite) if pd.notna(_composite) else 50, line_dash="dot", line_color="#888",
                      annotation_text=f"Composite: {_composite:.1f}" if pd.notna(_composite) else "")
    bar_fig.update_layout(
        height=260,
        margin=dict(l=10, r=10, t=20, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(range=[0, 100], gridcolor="rgba(255,255,255,0.08)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
    )
    st.plotly_chart(bar_fig, use_container_width=True)
else:
    st.caption("No sub-score breakdown available for this signal record.")

cm1, cm2, cm3 = st.columns(3)
cm1.metric("Composite score", f"{_composite:.1f}" if pd.notna(_composite) else "—")
cm2.metric("Sector macro modifier", f"{float(macro_modifier):.2f}×" if pd.notna(macro_modifier) else "—")
cm3.metric("Conviction", str(ticker_row.get("conviction") or "—"))

# Score history sparkline
score_hist = get_score_history(symbol=selected_ticker)
if score_hist is not None and not score_hist.empty and "score_date" in score_hist.columns:
    score_hist = score_hist.copy()
    score_hist["score_date"] = pd.to_datetime(score_hist["score_date"])
    sl_fig = go.Figure(go.Scatter(
        x=score_hist["score_date"],
        y=pd.to_numeric(score_hist["composite_score"], errors="coerce"),
        mode="lines+markers", line=dict(color="#1a73e8", width=2),
        name="Composite",
    ))
    sl_fig.update_layout(
        height=200, margin=dict(l=10, r=10, t=20, b=20),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(range=[0, 100], gridcolor="rgba(255,255,255,0.08)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
        title="Composite score over time",
    )
    st.plotly_chart(sl_fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Stage 4 — Predictor verdict
# ---------------------------------------------------------------------------

st.markdown("## 3. Predictor verdict")
st.caption(
    "Source: `predictor/predictions/{date}.json` (producer: predictor "
    "inference Lambda via `inference/daily_predict.py`). Stacked "
    "meta-ensemble — 3 Layer-1 specialized models + Layer-2 Ridge "
    "meta-learner over 12 features."
)

predictions_data = load_predictions_json(selected_date) or {}
ticker_pred = predictions_data.get(selected_ticker) if isinstance(predictions_data, dict) else None

if ticker_pred:
    direction = str(ticker_pred.get("direction") or ticker_pred.get("predicted_direction") or "—").upper()
    confidence = ticker_pred.get("confidence") or ticker_pred.get("max_class_prob")
    expected_move = ticker_pred.get("expected_move") or ticker_pred.get("predicted_alpha")
    veto_flag = ticker_pred.get("veto") or ticker_pred.get("vetoed")

    pcol1, pcol2, pcol3, pcol4 = st.columns(4)
    direction_color = {"UP": "#7fd17f", "DOWN": "#d06060", "FLAT": "#e0c050"}.get(direction, "#888")
    pcol1.markdown(
        f'<div style="text-align:center;"><div style="font-size:11px; color:#888;">Direction</div>'
        f'<div style="font-size:28px; font-weight:700; color:{direction_color};">{direction}</div></div>',
        unsafe_allow_html=True,
    )
    pcol2.metric("Confidence", f"{float(confidence)*100:.1f}%" if confidence is not None else "—")
    # Horizon-agnostic display: read horizon + domain from manifest so
    # any future horizon shift (e.g. 21d → 60d) flows through without
    # touching this string. Defaults are the active production state
    # post Track A cutover.
    _h = predictor_horizon_days()
    _domain_label = (
        "log-domain" if predictor_label_domain() == "canonical_log"
        else "arithmetic"
    )
    pcol3.metric(
        f"Expected {_h}d alpha",
        f"{float(expected_move)*100:+.2f}%" if expected_move is not None else "—",
        help=(
            f"Predictor's training horizon is {_h} trading days; alpha is "
            f"{_domain_label} per the manifest. Source: "
            f"`predictor/weights/meta/manifest.json`."
        ),
    )
    pcol4.metric("Veto active", "🚫 yes" if veto_flag else "✓ no")

    # L1 component votes if available
    components_data = ticker_pred.get("components") or ticker_pred.get("layer1") or {}
    if components_data and isinstance(components_data, dict):
        st.markdown("**Layer-1 component votes**")
        comp_rows = []
        for name, vals in components_data.items():
            if isinstance(vals, dict):
                comp_rows.append({
                    "Component": name,
                    "Score": vals.get("score") or vals.get("prediction"),
                    "Direction": vals.get("direction") or "—",
                })
        if comp_rows:
            st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

    # Top contributing features
    top_features = ticker_pred.get("top_features") or ticker_pred.get("feature_contributions")
    if top_features:
        st.markdown("**Top contributing features**")
        if isinstance(top_features, list):
            st.dataframe(pd.DataFrame(top_features), use_container_width=True, hide_index=True)
        else:
            st.json(top_features)
else:
    st.info(f"No predictor record for {selected_ticker} on {selected_date} — typical for tickers outside the prediction universe (held but not in the BUY candidate set).")

st.divider()

# ---------------------------------------------------------------------------
# Stage 5 — Veto gate decision
# ---------------------------------------------------------------------------

st.markdown("## 4. Veto gate decision")
st.caption(
    "Source: `predictor/predictions/{date}.json` `veto` field + "
    "`signals.json` `veto_status`. Veto fires when predictor confidence "
    "≥ threshold AND direction = DOWN, overriding any BUY signal."
)

veto_status = ticker_row.get("veto_status") or ticker_row.get("Veto") or ""
if veto_status and str(veto_status).upper().startswith("VETO"):
    st.error(f"🚫 **Vetoed.** {veto_status}")
elif ticker_pred and (ticker_pred.get("veto") or ticker_pred.get("vetoed")):
    st.error(f"🚫 **Vetoed by predictor.** {ticker_pred.get('veto_reason') or 'Predicted DOWN with high confidence.'}")
else:
    st.success("✓ Not vetoed. Signal flows through to position sizing.")

st.divider()

# ---------------------------------------------------------------------------
# Stage 6 — Position sizing + entry execution
# ---------------------------------------------------------------------------

st.markdown("## 5. Position sizing + entry execution")
st.caption(
    "Source: `order_books/{date}/summary.json` (producer: morning "
    "planner via `executor/main.py`) for sizing + entry trigger; "
    "`trades/trades_full.csv` (producer: `executor/trade_logger.py`) "
    "for fill price + slippage."
)

order_book = load_order_book_summary(selected_date) or {}
entries = []
if isinstance(order_book, dict):
    entries = order_book.get("entries") or order_book.get("approved") or []
ticker_entry = next((e for e in entries if e.get("ticker") == selected_ticker), None)

if ticker_entry:
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Shares", str(ticker_entry.get("shares", "—")))
    sc2.metric("Allocation %", f"{float(ticker_entry.get('allocation_pct', 0))*100:.2f}%" if ticker_entry.get("allocation_pct") else "—")
    sc3.metric("Trigger", str(ticker_entry.get("entry_trigger") or ticker_entry.get("trigger") or "—"))
    sc4.metric("Sizing rationale", str(ticker_entry.get("sizing_rationale") or "—"), label_visibility="collapsed")
else:
    st.info(f"{selected_ticker} not in {selected_date} order book entries (could be a HOLD/SELL or pre-existing position).")

# Trades for this ticker on this date or shortly after
trades_df = load_trades_full()
if trades_df is not None and not trades_df.empty:
    tt = trades_df.copy()
    tt["date"] = pd.to_datetime(tt.get("date"), errors="coerce")
    tt_window = tt[(tt["ticker"] == selected_ticker) & (tt["date"] >= pd.to_datetime(selected_date))].head(10)
    if not tt_window.empty:
        st.markdown("**Fills for this ticker (signal date forward)**")
        cols = [c for c in ["date", "action", "filled_shares", "fill_price", "price_at_order", "entry_trigger"] if c in tt_window.columns]
        st.dataframe(tt_window[cols].reset_index(drop=True), use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Stage 7 — EOD attribution
# ---------------------------------------------------------------------------

st.markdown("## 6. EOD attribution")
st.caption(
    "Source: `trades/eod_pnl.csv:positions_snapshot` (producer: "
    "`executor/eod_reconcile.py`). Most recent EOD row's snapshot."
)

eod_df = load_eod_pnl()
if eod_df is not None and not eod_df.empty and "positions_snapshot" in eod_df.columns:
    import json
    latest_eod = eod_df.iloc[-1]
    try:
        positions = json.loads(latest_eod["positions_snapshot"] or "{}")
        if isinstance(positions, dict) and selected_ticker in positions:
            pos = positions[selected_ticker]
            ec1, ec2, ec3, ec4 = st.columns(4)
            ec1.metric("Shares held", str(pos.get("shares", "—")))
            ec2.metric("Market value", f"${float(pos.get('market_value', 0)):,.0f}" if pos.get("market_value") else "—")
            ec3.metric("Avg cost", f"${float(pos.get('avg_cost', 0)):.2f}" if pos.get("avg_cost") else "—")
            unrealized = pos.get("unrealized_pnl")
            ec4.metric("Unrealized P&L", f"${float(unrealized):,.0f}" if unrealized is not None else "—")
            st.caption(f"Position state as of {pd.to_datetime(latest_eod.get('date')).strftime('%Y-%m-%d') if latest_eod.get('date') else 'latest EOD'}.")
        elif isinstance(positions, list):
            match = next((p for p in positions if p.get("ticker") == selected_ticker), None)
            if match:
                ec1, ec2, ec3 = st.columns(3)
                ec1.metric("Shares", str(match.get("shares", "—")))
                ec2.metric("Market value", f"${float(match.get('market_value', 0)):,.0f}")
                ec3.metric("Sector", str(match.get("sector", "—")))
            else:
                st.info(f"{selected_ticker} not in current positions snapshot — likely exited or never entered.")
        else:
            st.info(f"{selected_ticker} not in current positions snapshot.")
    except Exception as e:
        st.caption(f"Position parse failed: {e}")
else:
    st.caption("EOD positions snapshot not yet available.")

st.divider()

# ---------------------------------------------------------------------------
# Stage 8 — Backtester accuracy
# ---------------------------------------------------------------------------

st.markdown("## 7. Backtester accuracy update")
st.caption(
    "Source: `research.db:score_performance` (producer: signal_returns "
    "collector). For BUY signals, did the ticker beat SPY at 10-day and "
    "30-day windows?"
)

perf_df = get_score_performance()
if perf_df is not None and not perf_df.empty:
    ticker_perf = perf_df[perf_df.get("symbol") == selected_ticker] if "symbol" in perf_df.columns else perf_df.iloc[0:0]
    if not ticker_perf.empty:
        recent = ticker_perf.head(10).copy()
        if "score_date" in recent.columns:
            recent["score_date"] = pd.to_datetime(recent["score_date"]).dt.strftime("%Y-%m-%d")
        cols = [c for c in ["score_date", "composite_score", "signal", "beat_spy_10d", "beat_spy_30d", "fwd_return_10d", "fwd_return_30d"] if c in recent.columns]
        if cols:
            st.dataframe(recent[cols].reset_index(drop=True), use_container_width=True, hide_index=True)
        else:
            st.dataframe(recent.head(5), use_container_width=True, hide_index=True)
    else:
        st.info(f"No backtester accuracy rows for {selected_ticker} yet — typical for very recent signals (10d/30d windows haven't elapsed).")
else:
    st.caption("`score_performance` table not populated.")

st.divider()
st.caption(
    "All eight stages source from existing system outputs (Decision 11 "
    "of the presentation revamp plan). When a stage shows '—' or 'not "
    "available', the upstream pipeline either hasn't produced that "
    "datum yet (recent signal, window not elapsed) or this ticker "
    "doesn't pass that stage's filter (e.g., HOLD signals don't enter "
    "the order book)."
)

render_footer()
