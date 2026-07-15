"""
Performance — canonical portfolio-outcomes page (private console).

Merges the former **Portfolio** (#1), **EOD Report** (#19) and **Attribution
Heatmaps** (#37) pages into one window-aware surface, and retires the legacy
**Metrics** page. Every "what did the portfolio do" fact now has a single home;
**Execution** (#6, fill-quality) stays separate.

Owns ``url_path="eod-report"`` so the EOD email deep-link
(``…/eod-report?date=YYYY-MM-DD``) lands on the as-of day's report exactly as
before (guarded by ``tests/test_eod_report_page.py``). The single-day report is
always shown at the top + on the **Attribution** tab (the default view); the
**Window** selector (Today / 1W / 1M / YTD / Since inception) expands the
charts, per-stock alpha and per-position contribution to a longer period.

Lazy rendering: a ``segmented_control`` selects ONE tab and only that tab's body
(and its S3 reads / compute) runs — ``st.tabs`` renders every tab eagerly, which
would multiply the S3 / heatmap cost on each load. The ``?tab=`` query param is
preserved so a view is bookmarkable.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import (
    list_eod_report_dates,
    load_config,
    load_eod_pnl,
    load_eod_report,
    load_trades_full,
)
from charts.nav_chart import make_nav_chart
from charts.alpha_chart import make_alpha_chart
from charts.portfolio_chart import make_sector_allocation_chart
from shared.normalizers import to_decimal_series
from shared.accuracy_metrics import (
    compute_drawdown,
    compute_sharpe,
    find_drawdown_episodes,
)
from shared.position_pnl import (
    compute_position_lifecycles,
    parse_positions_snapshot,
)
from shared.attribution import (
    COL_CONTRIB_BPS,
    COL_RELATIVE,
    COL_RETURN,
    build_long_frame,
    build_weekly_frame,
    to_matrix,
)
from shared.constants import get_thresholds

_TH = get_thresholds()
_SHARPE_MIN_ROWS = int(_TH["sharpe_min_rows"])

WINDOWS = ["Today", "1W", "1M", "YTD", "Since inception"]
TABS = ["Overview", "Attribution", "Positions", "History"]


# ---------------------------------------------------------------------------
# Local formatters — shared.format_pct auto-rescales abs<2 values (it would turn
# an alpha of -0.70 into -70%), so percentages already in percent form are
# formatted directly here.
# ---------------------------------------------------------------------------
def _pct(v, decimals: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        return f"{float(v):+.{decimals}f}%"
    except (ValueError, TypeError):
        return "—"


def _usd(v, decimals: int = 0) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        return f"${float(v):,.{decimals}f}"
    except (ValueError, TypeError):
        return "—"


def _usd_signed(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        return f"{float(v):+,.0f}"
    except (ValueError, TypeError):
        return "—"


def _window_lo(as_of: str, window: str) -> str:
    """ISO lower-bound date for the window (inclusive). ISO strings compare
    lexicographically, so callers filter ``lo <= date <= as_of`` directly."""
    d = date.fromisoformat(as_of)
    if window == "Today":
        return as_of
    if window == "1W":
        return (d - timedelta(days=7)).isoformat()
    if window == "1M":
        return (d - timedelta(days=31)).isoformat()
    if window == "YTD":
        return date(d.year, 1, 1).isoformat()
    return "0000-01-01"  # Since inception


def _slice(eod_df: pd.DataFrame, as_of: str, window: str) -> pd.DataFrame:
    """Window slice of the (string-date) eod_pnl frame, ending at ``as_of``."""
    if eod_df is None or eod_df.empty or "date" not in eod_df.columns:
        return eod_df
    lo = _window_lo(as_of, window)
    d = eod_df["date"].astype(str)
    return eod_df[(d >= lo) & (d <= as_of)].copy()


def _heatmap(matrix: pd.DataFrame, *, color_label: str, value_fmt: str, unit: str):
    """(ticker × period) diverging RdYlGn heatmap anchored at zero."""
    fig = px.imshow(
        matrix, color_continuous_scale="RdYlGn", color_continuous_midpoint=0.0,
        aspect="auto", text_auto=value_fmt,
    )
    fig.update_traces(
        hovertemplate=f"%{{y}} · %{{x}}<br>%{{z:{value_fmt}}}{unit}<extra></extra>"
    )
    fig.update_xaxes(side="top", tickangle=-45, title=None)
    fig.update_yaxes(title=None)
    fig.update_layout(
        height=max(280, 26 * len(matrix.index) + 150),
        margin=dict(t=110, b=20, l=70, r=20),
        plot_bgcolor="white", paper_bgcolor="white",
        coloraxis_colorbar=dict(title=color_label),
    )
    return fig


# ===========================================================================
# Tab renderers — each runs ONLY when its tab is selected (lazy).
# ===========================================================================
def render_overview(eod_win: pd.DataFrame, window: str) -> None:
    st.subheader(f"Cumulative performance — {window}")
    if eod_win is None or eod_win.empty:
        st.info("No portfolio history in this window.")
        return

    dr = to_decimal_series(eod_win["daily_return_pct"])
    sr = to_decimal_series(eod_win["spy_return_pct"])
    port_cum = float((1 + dr).prod() - 1)
    spy_cum = float((1 + sr).prod() - 1)
    win_alpha = port_cum - spy_cum
    dd = compute_drawdown(dr)
    max_dd = float(dd.min()) if not dd.empty else None
    sharpe = compute_sharpe(dr, min_rows=_SHARPE_MIN_ROWS)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"Return ({window})", _pct(port_cum * 100))
    k2.metric(f"SPY ({window})", _pct(spy_cum * 100))
    k3.metric(f"Alpha ({window})", _pct(win_alpha * 100))
    k4.metric("Max Drawdown", _pct(max_dd * 100) if max_dd is not None else "—")

    st.plotly_chart(make_nav_chart(eod_win), use_container_width=True)

    st.subheader("Daily Alpha")
    st.plotly_chart(make_alpha_chart(eod_win), use_container_width=True)

    # Drawdown curve + episodes
    st.subheader("Drawdown")
    dd_pct = dd * 100
    fig = go.Figure(go.Scatter(
        x=pd.to_datetime(eod_win["date"]), y=dd_pct, fill="tozeroy", mode="lines",
        fillcolor="rgba(214,39,40,0.25)", line=dict(color="#d62728", width=1.5),
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Drawdown: %{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        height=260, plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(t=20, b=40, l=60, r=20), showlegend=False,
        yaxis=dict(title="Drawdown (%)", ticksuffix="%", zeroline=True,
                   zerolinecolor="rgba(0,0,0,0.3)", gridcolor="rgba(0,0,0,0.07)"),
        xaxis=dict(title="Date", gridcolor="rgba(0,0,0,0.07)"),
    )
    st.plotly_chart(fig, use_container_width=True)

    episodes = find_drawdown_episodes(dd, pd.to_datetime(eod_win["date"]))
    if episodes:
        st.dataframe(pd.DataFrame(episodes), use_container_width=True, hide_index=True)

    extra = []
    if sharpe is not None:
        extra.append(f"Sharpe (window): **{sharpe:.2f}**")
    extra.append(f"Trading days: **{len(eod_win)}**")
    st.caption(" · ".join(extra))


def render_attribution(report: dict, eod_win: pd.DataFrame, as_of: str, window: str) -> None:
    # --- Single-day waterfall for the as-of date (schema 1.0 + 2.0) ---
    st.subheader(f"Daily alpha attribution — {as_of}")
    attribution = report.get("alpha_attribution") if report else None
    if not attribution:
        st.info(
            "Attribution unavailable for this day (first trading day with no "
            "prior NAV, or no SPY reference)."
        )
    else:
        components = attribution.get("components", [])
        dollar_alpha = attribution.get("dollar_alpha")
        ties = attribution.get("ties_to_headline")
        residual = attribution.get("residual_usd", 0.0)
        tie_note = (
            f"Sleeves sum to the headline dollar-alpha "
            f"({_usd_signed(dollar_alpha)}; prior-NAV basis). "
        )
        if ties:
            st.caption(tie_note + "✅ Ties to headline.")
        else:
            st.warning(
                tie_note + f"⚠️ Does NOT tie — residual {_usd_signed(residual)}. "
                "Investigate the EOD reconciliation."
            )
        ordered = sorted(components, key=lambda c: c.get("contrib_usd", 0.0))
        labels = [c.get("label", "?") for c in ordered]
        values = [c.get("contrib_usd", 0.0) for c in ordered]
        colors = ["#006600" if v >= 0 else "#990000" for v in values]
        fig = go.Figure(go.Bar(
            x=values, y=labels, orientation="h", marker_color=colors,
            hovertemplate="%{y}: $%{x:,.0f}<extra></extra>",
        ))
        fig.update_layout(
            height=max(240, 28 * len(labels) + 80),
            margin=dict(t=10, b=30, l=110, r=20),
            plot_bgcolor="white", paper_bgcolor="white",
            xaxis=dict(title="Contribution to daily alpha ($)", zeroline=True,
                       zerolinecolor="rgba(0,0,0,0.4)", showgrid=True,
                       gridcolor="rgba(0,0,0,0.07)"),
            yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(fig, use_container_width=True)
        attr_df = pd.DataFrame([
            {
                "Component": c.get("label"), "Kind": c.get("kind"),
                "Contribution $": _usd_signed(c.get("contrib_usd")),
                "Contribution (bps)": (
                    f"{c.get('contrib_bps'):+.1f}"
                    if c.get("contrib_bps") is not None else "—"
                ),
            }
            for c in ordered
        ])
        st.dataframe(attr_df, use_container_width=True, hide_index=True)
        st.caption(
            "Sleeves: **position** (each holding vs SPY on its retained prior "
            "capital, including that name's own IB-mark-vs-settled-close basis "
            "slice — see \"of which pricing/timing $\" below), **rotation** "
            "(shares sold out today, plus exited names' basis slice), **cash** "
            "(interest minus SPY on idle cash), **pricing & timing** (only the "
            "leftover no stock could be attributed — normally ~$0), "
            "**unattributed** (true residual — not allocated to any stock; see "
            "config#2046)."
        )

        sectors = report.get("sector_attribution", []) or []
        if sectors:
            with st.expander("Sector attribution (this day)"):
                st.dataframe(pd.DataFrame([
                    {
                        "Sector": s.get("sector"),
                        "Weight": _pct(s.get("weight_pct"), 1),
                        "Contribution": _pct(s.get("contribution_pct")),
                        "Positions": s.get("positions"),
                    }
                    for s in sectors
                ]), use_container_width=True, hide_index=True)

    # --- Per-stock alpha across the window (daily + weekly) ---
    st.subheader(f"Per-stock alpha & return — {window}")
    long_df = build_long_frame(eod_win)
    if long_df.empty:
        st.info(
            "No per-position attribution history in this window. `eod_pnl` "
            "carries per-ticker daily returns from 2026-04-20 onward."
        )
        return
    weekly_df = build_weekly_frame(long_df)

    lens = st.radio(
        "Alpha lens", ["Market-relative return", "NAV-weighted contribution (bps)"],
        horizontal=True, key="perf_attr_lens",
        help=(
            "Market-relative = position return − SPY return, unweighted. "
            "NAV-weighted contribution (bps) sums across names to the portfolio's "
            "daily alpha (the EOD-report convention)."
        ),
    )
    if lens.startswith("Market-relative"):
        a_col, a_unit, a_fmt, a_lab = COL_RELATIVE, "%", ".1f", "rel %"
    else:
        a_col, a_unit, a_fmt, a_lab = COL_CONTRIB_BPS, " bps", ".0f", "bps"

    grain = st.segmented_control(
        "Grain", ["Daily", "Weekly"], default="Daily", key="perf_attr_grain",
    )
    if grain == "Weekly":
        if weekly_df.empty:
            st.info("Not enough history for a weekly view in this window.")
            return
        frame, period = weekly_df, "week"
    else:
        frame, period = long_df, "date"

    st.markdown(f"**Alpha — {lens}**")
    m = to_matrix(frame, a_col, period_col=period)
    if m.empty:
        st.warning("No data for this alpha lens.")
    else:
        st.plotly_chart(
            _heatmap(m, color_label=a_lab, value_fmt=a_fmt, unit=a_unit),
            use_container_width=True,
        )
    st.markdown("**Total return**")
    mr = to_matrix(frame, COL_RETURN, period_col=period)
    if mr.empty:
        st.warning("No return data.")
    else:
        st.plotly_chart(
            _heatmap(mr, color_label="ret %", value_fmt=".1f", unit="%"),
            use_container_width=True,
        )


def render_positions(report: dict, eod_win: pd.DataFrame, window: str) -> None:
    # --- Current holdings (as-of snapshot) ---
    st.subheader("Open positions (as-of day)")
    positions = (report.get("positions", []) if report else []) or []
    if positions:
        st.dataframe(pd.DataFrame([
            {
                "Ticker": p.get("ticker"), "Sector": p.get("sector"),
                "Shares": p.get("shares"), "Mkt Value": _usd(p.get("market_value")),
                "% NAV": _pct(p.get("pct_nav"), 1) if p.get("pct_nav") is not None else "—",
                "Day Ret %": _pct(p.get("daily_return_pct")),
                "Day Ret $": _usd_signed(p.get("daily_return_usd")),
                "α contrib (bps)": (
                    f"{p.get('alpha_contrib_bps'):+.1f}"
                    if p.get("alpha_contrib_bps") is not None else "—"
                ),
                # Schema 2.2 (config#2046): this name's own slice of the
                # pricing&timing (IB-mark-vs-settled) basis gap, already
                # folded into "α contrib" above — shown separately so a
                # large/small α contrib isn't mistaken for pure economic P&L.
                "of which pricing/timing $": (
                    _usd_signed(p.get("pricing_timing_contrib_usd"))
                    if p.get("pricing_timing_contrib_usd") else "—"
                ),
            }
            for p in positions
        ]), use_container_width=True, hide_index=True)
        rationales = [(p.get("ticker"), p.get("rationale")) for p in positions if p.get("rationale")]
        if rationales:
            with st.expander("Position rationale"):
                for ticker, rationale in rationales:
                    st.markdown(f"**{ticker}** — {rationale}")
    else:
        st.info("No open positions in this report.")

    # --- Per-name contribution over the window ---
    st.subheader(f"Per-name contribution — {window}")
    long_df = build_long_frame(eod_win)
    if long_df.empty:
        st.info("No per-position history in this window.")
    else:
        rows = []
        for ticker, g in long_df.groupby("ticker"):
            dr = g[COL_RETURN].dropna()
            ret = (float((1 + dr / 100.0).prod()) - 1) * 100 if len(dr) else None
            contrib = g[COL_CONTRIB_BPS].dropna()
            rows.append({
                "Ticker": ticker,
                "Total return": _pct(ret) if ret is not None else "—",
                "α contrib (bps)": (
                    f"{float(contrib.sum()):+.0f}" if len(contrib) else "—"
                ),
                "Days held": int(len(g)),
            })
        cdf = pd.DataFrame(rows).sort_values("Ticker")
        st.dataframe(cdf, use_container_width=True, hide_index=True)

    # --- Sector allocation (current snapshot) ---
    try:
        cfg = load_config()
        max_sector_pct = cfg.get("risk_limits", {}).get("max_sector_pct", 0.25)
        pos_df = parse_positions_snapshot(eod_win)
        if pos_df is not None and not pos_df.empty and "sector" in pos_df.columns:
            st.subheader("Sector allocation")
            col_chart, col_table = st.columns([2, 1])
            with col_chart:
                st.plotly_chart(make_sector_allocation_chart(pos_df), use_container_width=True)
            with col_table:
                mv = pd.to_numeric(pos_df["market_value"], errors="coerce").fillna(0)
                summ = pos_df.assign(market_value=mv).groupby("sector").agg(
                    Count=("ticker", "count"), Value=("market_value", "sum"),
                ).reset_index()
                tot = summ["Value"].sum()
                summ["Weight"] = (summ["Value"] / tot) if tot > 0 else 0
                summ["Limit"] = summ["Weight"].apply(lambda w: "LIMIT" if w > max_sector_pct else "")
                summ["Value"] = summ["Value"].apply(lambda v: f"${v:,.0f}")
                summ["Weight"] = summ["Weight"].apply(lambda w: f"{w:.1%}")
                st.dataframe(summ, use_container_width=True, hide_index=True)
    except Exception:  # noqa: BLE001 — allocation is secondary; never break the page
        pass


def render_history(report: dict, eod_win: pd.DataFrame, window: str) -> None:
    rt = report.get("roundtrip_stats") if report else None
    if rt and rt.get("n_roundtrips"):
        st.subheader("Roundtrip performance (all time)")
        r1, r2, r3, r4, r5 = st.columns(5)
        r1.metric("Closed Roundtrips", rt.get("n_roundtrips"))
        r2.metric("Avg Return", _pct(rt.get("avg_return_pct")))
        r3.metric("Avg Alpha vs SPY", _pct(rt.get("avg_alpha_pct")))
        win = rt.get("win_rate_vs_spy")
        r4.metric("Win Rate vs SPY", f"{win:.0f}%" if win is not None else "—")
        hold = rt.get("avg_hold_days")
        r5.metric("Avg Hold (days)", f"{hold}" if hold is not None else "—")

    st.subheader("Position lifecycle history")
    st.caption(
        "One row per opened position — entry trade + linked exits "
        "(REDUCE / EXIT / COVER) collapsed via `entry_trade_id`."
    )
    lifecycles_df = compute_position_lifecycles(load_trades_full())
    if lifecycles_df is None or lifecycles_df.empty:
        st.info("No position lifecycles to roll up yet (needs ≥1 ENTER in trades.db).")
    else:
        closed = lifecycles_df[lifecycles_df["status"] == "closed"]
        lcols = st.columns(4)
        lcols[0].metric("Closed", len(closed))
        lcols[1].metric("Open (partial)", len(lifecycles_df[lifecycles_df["status"] == "open_partial"]))
        lcols[2].metric("Open", len(lifecycles_df[lifecycles_df["status"] == "open"]))
        if not closed.empty:
            lcols[3].metric("Realized P&L (closed)", f"${float(closed['total_realized_pnl'].sum()):,.0f}")
        display_cols = [
            c for c in (
                "ticker", "sector", "entry_date", "exit_date", "holding_days",
                "entry_price", "shares_entered", "n_exits", "total_realized_pnl",
                "total_realized_return_pct", "total_realized_alpha_pct", "status",
            ) if c in lifecycles_df.columns
        ]
        ddf = lifecycles_df[display_cols].copy()
        for dc in ("entry_date", "exit_date"):
            if dc in ddf.columns:
                ddf[dc] = pd.to_datetime(ddf[dc], errors="coerce").dt.strftime("%Y-%m-%d")
        if "total_realized_pnl" in ddf.columns:
            ddf["total_realized_pnl"] = ddf["total_realized_pnl"].map(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
            )
        for pc in ("total_realized_return_pct", "total_realized_alpha_pct"):
            if pc in ddf.columns:
                ddf[pc] = ddf[pc].map(
                    lambda v: f"{v * 100:.1f}%" if pd.notna(v) and isinstance(v, (int, float)) else "—"
                )
        st.dataframe(ddf, hide_index=True, use_container_width=True)

    st.subheader(f"Daily history — {window}")
    if eod_win is not None and not eod_win.empty:
        hist = eod_win.sort_values("date", ascending=False)
        st.dataframe(pd.DataFrame({
            "Date": hist["date"].astype(str),
            "NAV": hist["portfolio_nav"].map(_usd) if "portfolio_nav" in hist else "—",
            "Return": hist["daily_return_pct"].map(_pct),
            "SPY": hist["spy_return_pct"].map(_pct),
            "Alpha": hist["daily_alpha_pct"].map(_pct) if "daily_alpha_pct" in hist else "—",
        }), use_container_width=True, hide_index=True)


# ===========================================================================
# Page
# ===========================================================================
st.title("Performance")
st.caption(
    "Canonical portfolio-outcomes report. The headline + **Attribution** tab "
    "show the as-of day (the source the EOD email links to); the **Window** "
    "selector expands the charts, per-stock alpha and contribution to a longer "
    "period. Daily-alpha attribution ties to the headline alpha exactly."
)

dates = list_eod_report_dates()
if not dates:
    st.info(
        "No EOD reports available yet (`consolidated/{date}/eod_report.json`). "
        "The first artifact is written by the next EOD reconciliation run."
    )
    st.stop()

# As-of date — honor ?date= deep-link from the EOD email.
qp_date = st.query_params.get("date")
default_idx = dates.index(qp_date) if qp_date in dates else 0
ctrl_l, ctrl_r = st.columns([1, 2])
with ctrl_l:
    as_of = st.selectbox("As-of date", dates, index=default_idx)
st.query_params["date"] = as_of
with ctrl_r:
    window = st.segmented_control(
        "Window", WINDOWS, default="1M", key="perf_window",
    ) or "1M"

report = load_eod_report(as_of)
if report is None:
    st.error(f"EOD report for {as_of} could not be loaded.")
    st.stop()

summary = report.get("summary", {})
for w in (report.get("data_warnings", []) or []):
    st.warning(f"⚠️ {w}")

# --- Always-on daily headline (the as-of day report) ---
_prov = bool(summary.get("spy_close_provisional"))
c1, c2, c3, c4 = st.columns(4)
c1.metric("NAV", _usd(summary.get("nav")))
c2.metric("Daily Return", _pct(summary.get("daily_return_pct")))
c3.metric(
    "SPY Return" + (" ⏳" if _prov else ""),
    _pct(summary.get("spy_return_pct")),
    help=("SPY close not yet settled — the T+1 reconcile_audit pass re-finalizes "
          "it from the settled close (config#1276)." if _prov else None),
)
c4.metric("Daily Alpha" + (" ⏳" if _prov else ""), _pct(summary.get("daily_alpha_pct")))
c5, c6, c7, c8 = st.columns(4)
c5.metric("Cash", _usd(summary.get("cash")))
c6.metric("Positions (MV)", _usd(summary.get("positions_mv")))
c7.metric("Unrealized P&L", _usd_signed(summary.get("unrealized_pnl")))
c8.metric("Realized P&L", _usd_signed(summary.get("realized_pnl")))
if _prov:
    st.caption(
        "⏳ **Provisional** — the SPY close (and therefore SPY Return / Daily "
        "Alpha) is not yet settled; it re-finalizes automatically on the next "
        "EOD run."
    )

st.divider()

# --- Lazy tab dispatch — only the active tab's body runs ---
qp_tab = st.query_params.get("tab")
tab = st.segmented_control(
    "View", TABS, default=qp_tab if qp_tab in TABS else "Attribution", key="perf_tab",
) or "Attribution"
st.query_params["tab"] = tab

eod_pnl = load_eod_pnl()
eod_win = _slice(eod_pnl, as_of, window) if eod_pnl is not None else None

if tab == "Overview":
    render_overview(eod_win, window)
elif tab == "Attribution":
    render_attribution(report, eod_win, as_of, window)
elif tab == "Positions":
    render_positions(report, eod_win, window)
elif tab == "History":
    render_history(report, eod_win, window)

gen = report.get("generated_at")
if gen:
    st.caption(
        f"Artifact generated_at: {gen} · schema v{report.get('schema_version', '?')}"
    )
