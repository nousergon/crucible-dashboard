"""
Signals & Research page — Full signal universe table with filters, sector ratings,
veto status, and a ticker drilldown that surfaces score history, conviction,
predictor probabilities, performance outcomes, and thesis timeline.

Merges the former Signals and Research pages (Phase 2 of dashboard-plan-optimized-260404).
"""

import logging
import sys
import os
from datetime import date

logger = logging.getLogger(__name__)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.signal_loader import (
    get_available_signal_dates,
    load_signals,
    signals_to_df,
    get_sector_ratings_df,
    compute_entrant_flow,
    get_entrant_detail_df,
)
from loaders.db_loader import (
    get_macro_snapshots,
    get_score_history,
    get_investment_thesis,
    query_research_db,
)
from loaders.s3_loader import load_predictions_json, load_predictor_params
from shared.constants import SIGNAL_COLORS, VETO_COLOR, get_thresholds
from shared.formatters import regime_label

_TH = get_thresholds()
_VETO_CONF_DEFAULT = _TH["veto_confidence"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BEAT_ICONS = {1: "✅", 0: "❌", True: "✅", False: "❌"}


def _beat_icon(val) -> str:
    if pd.isna(val):
        return "⏳"
    try:
        return BEAT_ICONS.get(val, "⏳")
    except (ValueError, TypeError):
        return "⏳"


def _render_signal_display(row: pd.Series) -> list[str]:
    """Return per-cell CSS styles for a signal row (veto override or signal-based color)."""
    veto_val = str(row.get("Veto", ""))
    if veto_val.startswith("VETOED"):
        return [f"background-color: {VETO_COLOR}" for _ in row]
    sig = str(row.get("signal", "HOLD")).upper()
    color = SIGNAL_COLORS.get(sig, SIGNAL_COLORS["HOLD"])
    return [f"background-color: {color}" for _ in row]


def _score_history_chart(score_df: pd.DataFrame, ticker: str) -> go.Figure:
    """Composite score line + sub-score faint lines + signal markers."""
    fig = go.Figure()

    if "score_date" in score_df.columns:
        score_df = score_df.copy()
        score_df["score_date"] = pd.to_datetime(score_df["score_date"])

    sub_colors = {"technical": "#aec7e8", "news": "#ffbb78", "research": "#98df8a"}
    for col, color in sub_colors.items():
        if col in score_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=score_df["score_date"],
                    y=pd.to_numeric(score_df[col], errors="coerce"),
                    mode="lines",
                    name=col.capitalize(),
                    line=dict(color=color, width=1.5, dash="dot"),
                    opacity=0.7,
                    hovertemplate=f"<b>%{{x|%Y-%m-%d}}</b><br>{col.capitalize()}: %{{y:.1f}}<extra></extra>",
                )
            )

    if "composite_score" in score_df.columns:
        fig.add_trace(
            go.Scatter(
                x=score_df["score_date"],
                y=pd.to_numeric(score_df["composite_score"], errors="coerce"),
                mode="lines+markers",
                name="Composite Score",
                line=dict(color="#1f77b4", width=2.5),
                marker=dict(size=6),
                hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Composite: %{y:.1f}<extra></extra>",
            )
        )

    if "signal" in score_df.columns and "composite_score" in score_df.columns:
        for sig, symbol, color in [
            ("ENTER", "triangle-up", "#2ca02c"),
            ("EXIT", "triangle-down", "#d62728"),
            ("REDUCE", "circle", "#ff7f0e"),
        ]:
            mask = score_df["signal"].str.upper() == sig
            if mask.any():
                fig.add_trace(
                    go.Scatter(
                        x=score_df.loc[mask, "score_date"],
                        y=pd.to_numeric(score_df.loc[mask, "composite_score"], errors="coerce"),
                        mode="markers",
                        name=sig,
                        marker=dict(symbol=symbol, size=12, color=color),
                        hovertemplate=f"<b>%{{x|%Y-%m-%d}}</b><br>Signal: {sig}<extra></extra>",
                    )
                )

    fig.update_layout(
        title=f"{ticker} — Score History",
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Score", range=[0, 100], showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=60, b=40, l=60, r=20),
        height=320,
    )
    return fig


def _conviction_history_chart(score_df: pd.DataFrame, ticker: str) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=pd.to_datetime(score_df.get("score_date", [])),
            y=pd.to_numeric(score_df["conviction"], errors="coerce"),
            mode="lines+markers",
            name="Conviction",
            line=dict(color="#9467bd", width=2.5),
            marker=dict(size=6),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Conviction: %{y:.1f}<extra></extra>",
        )
    )
    fig.update_layout(
        title=f"{ticker} — Conviction History",
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Conviction", range=[0, 100], showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
        showlegend=False,
        height=260,
    )
    return fig


def _predictor_probability_chart(pred: dict, ticker: str) -> go.Figure:
    p_up = pred.get("p_up", 0) or 0
    p_flat = pred.get("p_flat", 0) or 0
    p_down = pred.get("p_down", 0) or 0
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[p_up], y=[""], orientation="h",
        name="P(UP)", marker_color="#28a745",
        text=[f"P(UP): {p_up:.0%}"], textposition="inside",
    ))
    fig.add_trace(go.Bar(
        x=[p_flat], y=[""], orientation="h",
        name="P(FLAT)", marker_color="#6c757d",
        text=[f"P(FLAT): {p_flat:.0%}"], textposition="inside",
    ))
    fig.add_trace(go.Bar(
        x=[p_down], y=[""], orientation="h",
        name="P(DOWN)", marker_color="#dc3545",
        text=[f"P(DOWN): {p_down:.0%}"], textposition="inside",
    ))
    fig.update_layout(
        barmode="stack", title=f"{ticker} Predictor Probabilities",
        xaxis=dict(range=[0, 1], tickformat=".0%"),
        height=120, margin=dict(t=40, b=20, l=20, r=20),
        plot_bgcolor="white", paper_bgcolor="white",
        showlegend=True, legend=dict(orientation="h", y=-0.3),
    )
    return fig


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

st.title("Signals & Research")

# ---- Date selector ----
available_dates = get_available_signal_dates()
today_str = date.today().isoformat()

if not available_dates:
    from loaders.s3_loader import get_recent_s3_errors
    recent = get_recent_s3_errors()
    if recent:
        st.error(f"No signal dates — S3 error: {recent[-1].get('error_type', '?')}: {recent[-1].get('message', '')[:100]}")
    else:
        st.warning("No signal dates available in S3 — research pipeline may not have run yet.")
    st.stop()

default_idx = 0
if today_str in available_dates:
    default_idx = available_dates.index(today_str)

selected_date = st.selectbox(
    "Select Signal Date",
    options=available_dates,
    index=default_idx,
    help="Pick the date whose signals.json to view",
)

# ---- Load signals, predictions, and veto threshold ----
with st.spinner(f"Loading signals for {selected_date}..."):
    signals_data = load_signals(selected_date)
    predictions = load_predictions_json(selected_date)
    predictor_params = load_predictor_params()

veto_threshold = predictor_params.get("veto_confidence", _VETO_CONF_DEFAULT)

if not signals_data:
    st.warning(f"No signals available for {selected_date}.")
    st.stop()

# ---- Market regime chip row ----
macro_df = get_macro_snapshots()
if macro_df is not None and not macro_df.empty:
    macro_df["date"] = pd.to_datetime(macro_df["date"])
    day_macro = macro_df[macro_df["date"].dt.strftime("%Y-%m-%d") == selected_date]
    if day_macro.empty:
        past = macro_df[macro_df["date"].dt.strftime("%Y-%m-%d") <= selected_date]
        day_macro = past.tail(1)
    if not day_macro.empty:
        macro_row = day_macro.iloc[-1]
        regime = macro_row.get("regime", "—")
        vix = macro_row.get("vix", "—")
        yield_10yr = macro_row.get("yield_10yr", macro_row.get("10yr_yield", "—"))
        mc1, mc2, mc3, mc4 = st.columns(4)
        with mc1:
            st.metric("Regime", regime_label(regime))
        with mc2:
            try:
                st.metric("VIX", f"{float(vix):.1f}")
            except (ValueError, TypeError):
                st.metric("VIX", str(vix))
        with mc3:
            try:
                st.metric("10yr Yield", f"{float(yield_10yr):.2f}%")
            except (ValueError, TypeError):
                st.metric("10yr Yield", str(yield_10yr))
        with mc4:
            universe = signals_data.get("universe", [])
            st.metric("Universe Size", str(len(universe)))

st.divider()

# -----------------------------------------------------------------------
# Population Flow & New Entrants
# -----------------------------------------------------------------------
# Tracks whether the weekly run adds new names to the tracked population.
# A zero-add week is legitimate when no fresh candidate clears the CIO
# conviction bar (e.g. 2026-06-05: all 7 fresh names < conviction 40 vs the
# ~60 bar, mostly in underweight sectors) — but a sustained streak signals
# saturation. Previously this was invisible in the console.
st.subheader("Population Flow & New Entrants")
st.caption(
    "Net-new entrant = a candidate not held the prior week that the CIO "
    "advanced (including floor-forced). A zero-add week is defensible when "
    "the fresh slate is genuinely weak; watch the trend for a saturation streak."
)

_pop_target = _TH.get("population_target", 25)
_conv_bar = _TH.get("entrant_conviction_bar", 60)

# Prior signal date (next entry in the descending list) = new-vs-held baseline.
try:
    _sel_idx = available_dates.index(selected_date)
    _prior_date = (
        available_dates[_sel_idx + 1]
        if _sel_idx + 1 < len(available_dates)
        else None
    )
except ValueError:
    _prior_date = None

_flow_df = compute_entrant_flow(available_dates, weeks=12)
_this_row = None
if not _flow_df.empty:
    _match = _flow_df[_flow_df["date"] == selected_date]
    if not _match.empty:
        _this_row = _match.iloc[-1]

if _this_row is None:
    st.info(
        f"No CIO decision archive (archive/agent_runs/{selected_date}/cio.json) "
        "— new-entrant stats unavailable for this date."
    )
else:
    _nne = _this_row["net_new_entrants"]
    _nc = _this_row["new_candidates"]
    _cm = _this_row["new_conv_max"]
    _ps = _this_row["population_size"]
    fm1, fm2, fm3, fm4 = st.columns(4)
    with fm1:
        st.metric(
            "Net-new entrants",
            "—" if pd.isna(_nne) else int(_nne),
            help="Fresh names (not held last week) the CIO advanced this week.",
        )
    with fm2:
        st.metric(
            "Fresh candidates surfaced",
            "—" if pd.isna(_nc) else int(_nc),
            help="Candidates not in last week's population that the CIO evaluated.",
        )
    with fm3:
        st.metric(
            "Fresh-slate max conviction",
            "—" if pd.isna(_cm) else f"{_cm:.0f}",
            delta=None if pd.isna(_cm) else f"{_cm - _conv_bar:+.0f} vs ~{_conv_bar} bar",
            delta_color="normal",
            help=f"Highest conviction among fresh candidates. Entrants typically clear ~{_conv_bar}.",
        )
    with fm4:
        st.metric(
            "Population size",
            "—" if pd.isna(_ps) else int(_ps),
            delta=None if pd.isna(_ps) else f"{int(_ps) - _pop_target:+d} vs target {_pop_target}",
            delta_color="off",
            help=f"Held names vs target_size {_pop_target}. Above target → 0 open slots (saturation).",
        )

    if not pd.isna(_nne) and _nne == 0:
        _why = (
            f" Best fresh candidate scored {_cm:.0f} (bar ~{_conv_bar})."
            if not pd.isna(_cm)
            else ""
        )
        st.warning(
            f"**0 net-new entrants this week.**{_why} Defensible if the slate is "
            "genuinely weak — confirm via the detail table below; watch the trend "
            "for a saturation streak."
        )

# Weekly trend: net-new entrants (bars) + fresh-slate max conviction (line).
_disp = (
    _flow_df.dropna(subset=["net_new_entrants"])
    if not _flow_df.empty
    else _flow_df
)
if not _disp.empty:
    flow_fig = go.Figure()
    flow_fig.add_trace(
        go.Bar(
            x=_disp["date"],
            y=_disp["net_new_entrants"],
            name="Net-new entrants",
            marker_color="#2ca02c",
        )
    )
    flow_fig.add_trace(
        go.Scatter(
            x=_disp["date"],
            y=_disp["new_conv_max"],
            name="Fresh-slate max conviction",
            yaxis="y2",
            mode="lines+markers",
            line=dict(color="#1f77b4"),
        )
    )
    flow_fig.add_trace(
        go.Scatter(
            x=_disp["date"],
            y=[_conv_bar] * len(_disp),
            name=f"~{_conv_bar} entrant bar",
            yaxis="y2",
            mode="lines",
            line=dict(color="gray", dash="dot"),
        )
    )
    flow_fig.update_layout(
        height=320,
        margin=dict(t=30, b=0, l=0, r=0),
        yaxis=dict(title="Net-new entrants"),
        yaxis2=dict(
            title="Max conviction", overlaying="y", side="right", range=[0, 100]
        ),
        legend=dict(orientation="h", y=1.18),
    )
    st.plotly_chart(flow_fig, use_container_width=True)

# This-week fresh-candidate detail (advanced + rejected new names).
_detail = get_entrant_detail_df(selected_date, _prior_date)
if not _detail.empty:
    st.markdown("**This week's fresh candidates** (not held last week)")
    st.dataframe(_detail, use_container_width=True, hide_index=True)
elif _this_row is not None:
    st.caption(
        "No fresh candidates surfaced this week — all CIO candidates were incumbents."
    )

st.divider()

# ---- Build signal DataFrame ----
sig_df = signals_to_df(signals_data)

if sig_df.empty:
    st.info("Signal universe is empty for this date.")
    st.stop()

# -----------------------------------------------------------------------
# Filters
# -----------------------------------------------------------------------
st.subheader("Filters")

filter_col1, filter_col2, filter_col3 = st.columns([2, 2, 2])

with filter_col1:
    sectors = sorted(sig_df["sector"].dropna().unique().tolist()) if "sector" in sig_df.columns else []
    selected_sectors = st.multiselect("Sector", options=sectors, default=[])

with filter_col2:
    signal_types = sorted(sig_df["signal"].dropna().unique().tolist()) if "signal" in sig_df.columns else []
    selected_signals = st.multiselect("Signal Type", options=signal_types, default=[])

with filter_col3:
    min_score = st.slider(
        "Min Score",
        min_value=0,
        max_value=100,
        value=0,
        step=5,
    )

filtered_df = sig_df.copy()
if selected_sectors:
    filtered_df = filtered_df[filtered_df["sector"].isin(selected_sectors)]
if selected_signals:
    filtered_df = filtered_df[filtered_df["signal"].isin(selected_signals)]
if "score" in filtered_df.columns:
    filtered_df = filtered_df[pd.to_numeric(filtered_df["score"], errors="coerce").fillna(0) >= min_score]

filtered_df = filtered_df.sort_values("score", ascending=False).reset_index(drop=True)

if "stale" in filtered_df.columns:
    filtered_df["stale"] = filtered_df["stale"].apply(lambda x: "⚠" if x else "")

st.caption(f"Showing {len(filtered_df)} of {len(sig_df)} signals")

# -----------------------------------------------------------------------
# Main signal table
# -----------------------------------------------------------------------
st.subheader("Signal Table")

if predictions:
    filtered_df["Prediction"] = filtered_df["ticker"].map(
        lambda t: {"UP": "UP ↑", "DOWN": "DOWN ↓", "FLAT": "FLAT →"}.get(
            (predictions.get(t) or {}).get("predicted_direction", ""), ""
        )
    )
    filtered_df["Confidence"] = filtered_df["ticker"].map(
        lambda t: (predictions.get(t) or {}).get("prediction_confidence")
    )
    filtered_df.loc[
        pd.to_numeric(filtered_df["Confidence"], errors="coerce").fillna(0) < veto_threshold,
        "Confidence"
    ] = None

    def _veto_status(row):
        pred = predictions.get(row.get("ticker", ""), {})
        if not pred:
            return ""
        direction = pred.get("predicted_direction", "")
        conf = pred.get("prediction_confidence") or 0.0
        if direction == "DOWN" and conf >= veto_threshold:
            return f"VETOED ({conf:.0%})"
        return ""

    filtered_df["Veto"] = filtered_df.apply(_veto_status, axis=1)

    enter_signals = filtered_df[filtered_df["signal"] == "ENTER"]
    vetoed_count = enter_signals["Veto"].str.startswith("VETOED").sum() if not enter_signals.empty else 0
    total_enter = len(enter_signals)
    if vetoed_count > 0:
        st.warning(f"{vetoed_count} of {total_enter} ENTER signals currently vetoed by predictor (threshold: {veto_threshold:.0%})")

display_cols = [
    c for c in [
        "ticker", "sector", "signal", "score", "conviction",
        "rating", "technical", "news", "research",
        "Prediction", "Confidence", "Veto",
        "price_target_upside", "stale", "thesis_summary"
    ]
    if c in filtered_df.columns
]
display_df = filtered_df[display_cols].copy()

styled = display_df.style.apply(_render_signal_display, axis=1)
for col in ["score", "conviction", "technical", "news", "research"]:
    if col in display_df.columns:
        styled = styled.format({col: "{:.1f}"}, na_rep="—")
if "price_target_upside" in display_df.columns:
    styled = styled.format({"price_target_upside": "{:.1%}"}, na_rep="—")

st.dataframe(styled, use_container_width=True, hide_index=True)

# -----------------------------------------------------------------------
# Ticker Drilldown — absorbs the former Research page content
# -----------------------------------------------------------------------
st.divider()
st.subheader("Ticker Drilldown")

tickers = sorted(filtered_df["ticker"].dropna().unique().tolist()) if "ticker" in filtered_df.columns else []
selected_ticker = st.selectbox(
    "Select ticker for detail view",
    options=[""] + tickers,
    help="Score history, conviction, predictor probabilities, performance outcomes, and thesis timeline",
)

if selected_ticker:
    ticker_row = filtered_df[filtered_df["ticker"] == selected_ticker]
    ticker_row = ticker_row.iloc[0] if not ticker_row.empty else None

    # --- Thesis summary from signal row ---
    if ticker_row is not None:
        thesis = ticker_row.get("thesis_summary", "")
        if thesis:
            st.markdown(f"**Thesis Summary:** {thesis}")

        # Sub-score bar chart (current snapshot)
        sub_scores = {}
        for s in ["technical", "news", "research"]:
            val = ticker_row.get(s)
            if pd.notna(val):
                try:
                    sub_scores[s.capitalize()] = float(val)
                except (ValueError, TypeError):
                    pass

        if sub_scores:
            sub_fig = go.Figure(
                go.Bar(
                    x=list(sub_scores.values()),
                    y=list(sub_scores.keys()),
                    orientation="h",
                    marker_color=["#1f77b4", "#ff7f0e", "#2ca02c"],
                    text=[f"{v:.1f}" for v in sub_scores.values()],
                    textposition="outside",
                )
            )
            sub_fig.update_layout(
                title=f"{selected_ticker} Sub-Score Breakdown (current)",
                xaxis=dict(title="Score", range=[0, 100]),
                yaxis=dict(title=""),
                plot_bgcolor="white",
                paper_bgcolor="white",
                height=200,
                margin=dict(t=40, b=30, l=100, r=60),
            )
            st.plotly_chart(sub_fig, use_container_width=True)

    # --- Predictor probabilities (current snapshot) ---
    pred = predictions.get(selected_ticker, {}) if predictions else {}
    if pred:
        confidence = pred.get("prediction_confidence", 0) or 0
        direction = pred.get("predicted_direction", "")
        pred_fig = _predictor_probability_chart(pred, selected_ticker)
        st.plotly_chart(pred_fig, use_container_width=True)

        if confidence >= veto_threshold:
            p_up = pred.get("p_up", 0) or 0
            p_down = pred.get("p_down", 0) or 0
            modifier = (p_up - p_down) * 10.0 * confidence
            sign = "+" if modifier >= 0 else ""
            st.caption(
                f"Predictor: **{direction}** (confidence {confidence:.0%}) — "
                f"Score modifier applied: **{sign}{modifier:.1f} pts**"
            )
        else:
            st.caption(
                f"Predictor: {direction or '—'} (confidence {confidence:.0%}) — "
                f"Modifier skipped (confidence < {veto_threshold:.0%})"
            )

    # --- Full score history from research DB (with sub-scores + signal markers) ---
    with st.spinner(f"Loading history for {selected_ticker}..."):
        full_score_df = query_research_db(
            "SELECT * FROM score_performance WHERE symbol = ? ORDER BY score_date",
            params=(selected_ticker,),
        )
        if full_score_df.empty:
            full_score_df = get_score_history(selected_ticker)
        thesis_df = get_investment_thesis(selected_ticker)

    if full_score_df is not None and not full_score_df.empty:
        full_score_df["score_date"] = pd.to_datetime(full_score_df["score_date"])
        full_score_df = full_score_df.sort_values("score_date")

        st.plotly_chart(_score_history_chart(full_score_df, selected_ticker), use_container_width=True)

        if "conviction" in full_score_df.columns and full_score_df["conviction"].notna().any():
            st.plotly_chart(_conviction_history_chart(full_score_df, selected_ticker), use_container_width=True)

        # --- Performance outcomes table ---
        st.markdown("**Performance Outcomes**")
        outcome_cols = [
            c for c in [
                "score_date", "composite_score",
                "beat_spy_21d",
                "return_21d", "spy_21d_return"
            ]
            if c in full_score_df.columns
        ]
        if outcome_cols:
            outcome_df = full_score_df[outcome_cols].copy().sort_values("score_date", ascending=False)
            for col in ["beat_spy_21d"]:
                if col in outcome_df.columns:
                    outcome_df[col] = outcome_df[col].apply(_beat_icon)
            for col in ["return_21d", "spy_21d_return"]:
                if col in outcome_df.columns:
                    outcome_df[col] = pd.to_numeric(outcome_df[col], errors="coerce").apply(
                        lambda x: f"{x*100:+.2f}%" if pd.notna(x) else "⏳"
                    )
            st.dataframe(outcome_df, use_container_width=True, hide_index=True)
    else:
        st.info(f"No score history found for {selected_ticker} in research DB.")

    # --- Thesis timeline ---
    st.markdown("**Thesis Timeline**")
    if thesis_df is not None and not thesis_df.empty:
        date_col = next((c for c in ["date", "created_at", "updated_at", "thesis_date"] if c in thesis_df.columns), None)
        if date_col:
            thesis_df[date_col] = pd.to_datetime(thesis_df[date_col])
            thesis_df = thesis_df.sort_values(date_col, ascending=False)

        for _, row in thesis_df.iterrows():
            date_val = str(row.get(date_col, "Unknown date")) if date_col else "Unknown date"
            thesis_text = row.get("thesis_summary", row.get("thesis", ""))
            signal = row.get("signal", "")
            score = row.get("composite_score", row.get("score", ""))

            header = f"{date_val}"
            if signal:
                header += f" — {signal}"
            if pd.notna(score) and score != "":
                try:
                    header += f" (Score: {float(score):.1f})"
                except (ValueError, TypeError):
                    pass

            with st.expander(header):
                if thesis_text:
                    st.write(thesis_text)
                else:
                    st.write("No thesis text available.")
    else:
        st.info(f"No investment thesis records found for {selected_ticker}.")

# -----------------------------------------------------------------------
# Sector Ratings
# -----------------------------------------------------------------------
st.divider()
st.subheader("Sector Ratings")

sector_df = get_sector_ratings_df(signals_data)
if not sector_df.empty:
    st.dataframe(sector_df, use_container_width=True, hide_index=True)
else:
    st.info("No sector ratings in this signal file.")
