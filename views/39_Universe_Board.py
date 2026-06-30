"""
Universe Board — Alpha Engine (private console)

The FULL ~900-name S&P 500+400 scanner universe in one navigable table:
per-stock **attractiveness** (equal-weight blend of the 6 factor pillars),
each pillar score, the scanner gate outcome, and the raw valuation /
fundamental / technical metrics — sortable and filterable by attractiveness,
by any pillar, by any metric range, by sector, by country, and by gate status.

Where the Scanner page shows the funnel (gates + pass/fail by sector), this
page is the screener: rank and slice the whole universe to find names, not
just the ~60 that survived the filter. Reads the typed
``scanner/universe/latest.json`` artifact produced by crucible-research
``scoring/universe_board.py`` (no LLM call, no cost).

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome — no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.attractiveness_trends import ticker_series
from loaders.s3_loader import load_attractiveness_history, load_universe_board
from loaders.universe_board import (
    GATE_STAGE_LABELS,
    METRIC_LABELS,
    PILLARS,
    board_meta,
    contributions_df,
    flatten_board,
    gate_trace_df,
    index_by_ticker,
)

st.markdown("### 🌌 Universe Board")
st.caption(
    "The full S&P 500+400 (~900 names): attractiveness, factor pillars, and raw "
    "metrics for every stock the scanner screens — filter by metric, sector, "
    "country, or gate. Read from the recorded scan (no LLM call, no cost)."
)

board = load_universe_board()
if not board or not board.get("stocks"):
    st.warning(
        "No universe board published yet. It is produced on each Saturday "
        "research cycle by crucible-research (`scoring/universe_board.py` → "
        "`scanner/universe/latest.json`)."
    )
    st.stop()

meta = board_meta(board)
as_of = meta["as_of"] or "—"
method = meta["attractiveness_method"]
weights = meta["pillar_weights"]
gate_config = meta["gate_config"]
by_ticker = index_by_ticker(board)

# Flatten the per-stock records into a numeric-coerced display DataFrame
# (pure transform lives in loaders/universe_board.py — see the consumer
# contract test).
_PILLARS = PILLARS
df = flatten_board(board)

# ── Headline ────────────────────────────────────────────────────────────────
n = len(df)
n_pass = int((df["gate"] == "PASS").sum())
avg_attr = df["attractiveness"].mean()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Universe", n)
m2.metric("Passed quant gate", n_pass)
m3.metric("Avg attractiveness", f"{avg_attr:.1f}" if pd.notna(avg_attr) else "—")
m4.metric("As of", as_of)

if method == "sector_neutral_zscore_percentile":
    st.caption(
        "**Attractiveness (0–100)** = cross-sectional **percentile of a "
        "sector-neutral, winsorized z-score blend** of the 6 factor pillars "
        "(Grinold-Kahn). The pillars are already within-sector percentile ranks; "
        "the blend is re-standardized so the score uses the full 0–100 range. "
        "Select a ticker below to see each pillar's contribution and how the "
        "scanner gated it."
    )
    if weights:
        wtxt = " · ".join(f"{p[:4].title()} {weights.get(p, 0) * 100:.0f}%" for p in _PILLARS)
        st.caption(f"Pillar weights: {wtxt}")
else:
    # schema_version=1 fallback caption.
    st.caption(f"Attractiveness = {method.replace('_', ' ')} (0–100). Higher is more attractive.")

# Scanner gate thresholds used this cycle (transparency: the actual numbers).
if gate_config:
    with st.expander("🚪 Scanner gates this cycle (thresholds)"):
        gc = gate_config
        st.markdown(
            f"- **Liquidity** — avg 20d volume ≥ `{gc.get('min_avg_volume')}`"
            + (f", price ≥ `${gc.get('min_price')}`" if (gc.get('min_price') or 0) > 0 else "")
            + f"\n- **Volatility** — ATR% ≤ `{gc.get('max_atr_pct')}` (momentum) / "
            f"`{gc.get('deep_value_max_atr_pct')}` (deep value)"
            f"\n- **Momentum path** — tech_score ≥ `{gc.get('tech_score_min')}` and "
            f"price-vs-MA200 > `{gc.get('momentum_ma200_floor_pct')}`%"
            f"\n- **Deep-value path** — {'enabled' if gc.get('deep_value_path_enabled') else 'disabled'}, "
            f"RSI < `{gc.get('deep_value_max_rsi')}`"
            f"\n- **Rank cutoff** — top `{gc.get('momentum_top_n')}` by tech_score "
            f"(+ up to `{gc.get('deep_value_max_candidates')}` deep-value)"
        )

st.divider()

# ── Filters ─────────────────────────────────────────────────────────────────
f1, f2, f3 = st.columns([2, 2, 1])
with f1:
    sectors = sorted(df["sector"].dropna().unique().tolist())
    pick_sectors = st.multiselect("Sectors", sectors, default=sectors)
with f2:
    countries = sorted(df["country"].dropna().unique().tolist())
    pick_countries = st.multiselect("Countries", countries, default=countries)
with f3:
    gate_choice = st.radio("Gate", ["All", "Passed", "Failed"], horizontal=False)

search = st.text_input("Search tickers (comma-separated)", "")
attr_min = st.slider("Minimum attractiveness", 0, 100, 0)

# Dynamic "filter by any metric range" — the flexible multi-metric filter.
_NUMERIC_COLS = [
    c for c in df.columns
    if c not in ("attractiveness", "attractiveness_raw")
    and pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any()
]
_LABELS = METRIC_LABELS
with st.expander("Advanced metric filters — filter by any metric range"):
    st.caption("Pick one or more pillars / metrics and set a range. All filters combine (AND).")
    pc = st.columns(3)
    pillar_min = {}
    for i, p in enumerate(_PILLARS):
        with pc[i % 3]:
            pillar_min[p] = st.slider(f"Min {p}", 0, 100, 0, key=f"pmin_{p}")
    add_metrics = st.multiselect(
        "Add metric range filters",
        [c for c in _NUMERIC_COLS if c not in _PILLARS + ["focus", "tech"]],
        format_func=lambda c: _LABELS.get(c, c),
    )
    metric_ranges = {}
    for c in add_metrics:
        lo, hi = float(df[c].min()), float(df[c].max())
        if lo == hi:
            hi = lo + 1.0
        metric_ranges[c] = st.slider(
            _LABELS.get(c, c), lo, hi, (lo, hi), key=f"mrange_{c}"
        )

# ── Apply filters ───────────────────────────────────────────────────────────
view = df[df["sector"].isin(pick_sectors) & df["country"].isin(pick_countries)]
if gate_choice == "Passed":
    view = view[view["gate"] == "PASS"]
elif gate_choice == "Failed":
    view = view[view["gate"] == "FAIL"]
if search.strip():
    wanted = {t.strip().upper() for t in search.split(",") if t.strip()}
    view = view[view["ticker"].str.upper().isin(wanted)]
view = view[view["attractiveness"].fillna(-1) >= attr_min]
for p, lo in pillar_min.items():
    if lo > 0:
        view = view[view[p].fillna(-1) >= lo]
for c, (lo, hi) in metric_ranges.items():
    view = view[view[c].between(lo, hi)]  # NaN fails between → excluded when filtering on a metric

# ── Render ──────────────────────────────────────────────────────────────────
st.caption(f"{len(view)} of {n} names match.")

_PCT_COLS = ["fcf_yield", "div_yield", "roe", "gross_margin", "rev_gr_3y", "eps_gr_3y",
             "mom_20d", "ret_60d", "ret_120d", "vol_20d", "atr_pct", "dist_52w_hi", "vs_ma200"]
col_config = {
    "attractiveness": st.column_config.ProgressColumn(
        "Attract.", min_value=0, max_value=100, format="%.1f",
        help="Cross-sectional percentile of the sector-neutral z-score blend of "
             "the 6 factor pillars (0–100). Higher = more attractive.",
    ),
    "tradeability": st.column_config.ProgressColumn(
        "Tradeab.", min_value=0, max_value=100, format="%.1f",
        help="Cost-to-access percentile (0–100, higher = cheaper to trade). An "
             "INDEPENDENT √-impact cost score, never blended into attractiveness "
             "(ARCHITECTURE §43): a great-but-expensive name and a mediocre-but-"
             "cheap name stay distinguishable. Blank = no liquidity/vol coverage.",
    ),
    "expected_cost_bps": st.column_config.NumberColumn(
        "RT cost (bps)", format="%.1f",
        help="Estimated round-trip transaction cost in bps at the reference trade "
             "size (half-spread + √-impact + commission)."),
    "gate_stage": st.column_config.TextColumn(
        "Gate", help="Terminal scanner funnel stage (passed, or where it dropped)."),
    "price": st.column_config.NumberColumn("Price", format="$%.2f"),
    "mkt_cap": st.column_config.NumberColumn("Mkt Cap", format="compact"),
    "avg_vol": st.column_config.NumberColumn("Avg Vol", format="compact"),
    "pe": st.column_config.NumberColumn("P/E", format="%.1f"),
    "pb": st.column_config.NumberColumn("P/B", format="%.2f"),
    "debt_to_equity": st.column_config.NumberColumn("D/E", format="%.2f"),
    "current_ratio": st.column_config.NumberColumn("Curr.", format="%.2f"),
    "payout": st.column_config.NumberColumn("Payout", format="%.2f"),
    "rsi": st.column_config.NumberColumn("RSI", format="%.0f"),
    "beta": st.column_config.NumberColumn("Beta", format="%.2f"),
}
for p in _PILLARS + ["focus", "tech"]:
    col_config[p] = st.column_config.NumberColumn(p[:5].title(), format="%.0f")
for c in _PCT_COLS:
    col_config[c] = st.column_config.NumberColumn(_LABELS.get(c, c), format="percent")

view_display = view.sort_values("attractiveness", ascending=False, na_position="last").copy()
if "gate_stage" in view_display.columns:
    view_display["gate_stage"] = view_display["gate_stage"].map(
        lambda s: GATE_STAGE_LABELS.get(s, s) if pd.notna(s) else s
    )
st.dataframe(
    view_display, use_container_width=True, hide_index=True, height=620,
    column_config=col_config,
)

st.caption(
    "Metrics are display-raw (valuation multiples denormalized from the predictor "
    "feature store). Blank = coverage gap (never a guessed value). Pillars: Quality · "
    "Value · Momentum · Growth · Stewardship · Defensiveness. Sort any column by clicking its header."
)

# ── Per-stock detail: WHY it scores + HOW the scanner judged it ──────────────
st.divider()
st.markdown("#### 🔬 Stock detail — why this score, and how the scanner judged it")
_match = view["ticker"].dropna().tolist()
if not _match:
    st.caption("No names in the current filter to inspect.")
else:
    pick = st.selectbox("Ticker", _match, key="ub_detail_ticker")
    stock = by_ticker.get(pick) or {}
    drow = view[view["ticker"] == pick]
    dc1, dc2 = st.columns(2)
    with dc1:
        st.markdown("**Attractiveness — pillar contributions**")
        cdf = contributions_df(stock)
        if cdf.empty:
            st.caption("No factor pillars available for this name (attractiveness is N/A).")
        else:
            raw = stock.get("attractiveness_raw")
            score = stock.get("attractiveness_score")
            st.caption(
                f"Score **{score:.1f}** (percentile) · raw z-blend "
                f"**{raw:+.3f}** = sum of the signed contributions below "
                "(each = pillar weight × its winsorized z-score, renormalized over "
                "available pillars). Green pulls the score up, red pulls it down."
            )
            st.dataframe(
                cdf.set_index("pillar"),
                use_container_width=True,
                column_config={"contribution": st.column_config.NumberColumn(
                    "Contribution", format="%+.3f")},
            )
            st.bar_chart(cdf.set_index("pillar")["contribution"], horizontal=True)
    with dc2:
        st.markdown("**Scanner gate trace**")
        gstage = stock.get("gate_stage")
        st.caption(f"Terminal stage: **{GATE_STAGE_LABELS.get(gstage, gstage or '—')}**")
        gtdf = gate_trace_df(stock)
        if gtdf.empty:
            st.caption("No gate trace recorded for this name.")
        else:
            st.dataframe(
                gtdf, use_container_width=True, hide_index=True,
                column_config={
                    "value": st.column_config.NumberColumn("Value", format="%.4g"),
                    "threshold": st.column_config.NumberColumn("Threshold", format="%.4g"),
                },
            )
            st.caption(
                "Each gate shows the stock's value vs the threshold the scanner used "
                "this cycle. A name can clear every value-gate yet drop at the rank "
                "cutoff (only the top-N by tech_score make the basket)."
            )

    # Attractiveness over time (sparkline from the history parquet) — how this
    # name's attractiveness is trending. Full trend signal: Attractiveness Trends.
    _hist = load_attractiveness_history()
    _series = ticker_series(_hist, pick) if not _hist.empty else _hist
    if _series is not None and not _series.empty and len(_series) > 1:
        st.markdown(f"**{pick} — attractiveness over time**")
        st.line_chart(_series[["attractiveness_score"]], height=200)
        st.caption("Weekly attractiveness percentile. Trend signal + pre-repricing leaderboard: "
                   "the **Attractiveness Trends** page.")
