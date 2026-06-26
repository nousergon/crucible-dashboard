"""
EOD Report — Alpha Engine (private console).

Renders the structured ``consolidated/{date}/eod_report.json`` artifact
(producer: alpha-engine ``executor/eod_report.py``) as the canonical
end-of-day report. This REPLACES the former "EOD Reconcile (archive)" page,
which re-rendered the emailed ``eod.html`` verbatim and therefore inherited
its bugs (the sign-flipping "α % of Total" column and a positions-table alpha
total that never reconciled with the NAV-based headline).

The daily-alpha attribution shown here is the prior-NAV-basis decomposition
that ties to the headline alpha exactly: per-position + cash & rotation +
unattributed contributions sum to ``prior_nav × (daily_return − spy_return)``.

Deep-link: the EOD email links to ``…/eod-report?date=YYYY-MM-DD`` — the
``url_path`` is pinned in app.py and guarded by
``tests/test_eod_report_page.py`` against the executor's emailer slug.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import list_eod_report_dates, load_eod_report


# ---------------------------------------------------------------------------
# Local formatters — note shared.format_pct auto-rescales abs<2 values
# (it would turn an alpha of -0.70 into -70%), so percentages already in
# percent form are formatted directly here.
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


st.title("EOD Report")
st.caption(
    "Canonical end-of-day report — rendered from the "
    "`consolidated/{date}/eod_report.json` artifact (the source the EOD email "
    "links to). Daily-alpha attribution is the prior-NAV-basis decomposition "
    "that ties to the headline alpha."
)

# --- Date selection (honor ?date= deep-link from the EOD email) -------------
dates = list_eod_report_dates()
if not dates:
    st.info(
        "No EOD reports available yet "
        "(`consolidated/{date}/eod_report.json`). The first artifact is "
        "written by the next EOD reconciliation run."
    )
    st.stop()

qp_date = st.query_params.get("date")
default_idx = dates.index(qp_date) if qp_date in dates else 0
selected_date = st.selectbox("Trading day", dates, index=default_idx)
st.query_params["date"] = selected_date

report = load_eod_report(selected_date)
if report is None:
    st.error(f"EOD report for {selected_date} could not be loaded.")
    st.stop()

summary = report.get("summary", {})
warnings = report.get("data_warnings", []) or []

# --- Data warnings (loud) ---------------------------------------------------
for w in warnings:
    st.warning(f"⚠️ {w}")

# --- Daily Summary ----------------------------------------------------------
st.header("Daily Summary")
_provisional = bool(summary.get("spy_close_provisional"))
c1, c2, c3, c4 = st.columns(4)
c1.metric("NAV", _usd(summary.get("nav")))
c2.metric("Daily Return", _pct(summary.get("daily_return_pct")))
c3.metric(
    "SPY Return" + (" ⏳" if _provisional else ""),
    _pct(summary.get("spy_return_pct")),
    help=("SPY close not yet settled — read at ~4:20pm ET; the T+1 reconcile_audit "
          "pass re-finalizes it from the settled close (config#1276)." if _provisional else None),
)
c4.metric("Daily Alpha" + (" ⏳" if _provisional else ""), _pct(summary.get("daily_alpha_pct")))
if _provisional:
    st.caption(
        "⏳ **Provisional** — the SPY close (and therefore SPY Return / Daily Alpha) "
        "is not yet settled. These re-finalize automatically on the next EOD run."
    )

c5, c6, c7, c8 = st.columns(4)
c5.metric("Cash", _usd(summary.get("cash")))
c6.metric("Positions (MV)", _usd(summary.get("positions_mv")))
c7.metric("Unrealized P&L", _usd_signed(summary.get("unrealized_pnl")))
c8.metric("Realized P&L", _usd_signed(summary.get("realized_pnl")))

# --- Daily Alpha Attribution ------------------------------------------------
st.header("Daily Alpha Attribution")
attribution = report.get("alpha_attribution")
if not attribution:
    st.info(
        "Attribution unavailable for this day (first trading day with no prior "
        "NAV, or no SPY reference)."
    )
else:
    components = attribution.get("components", [])
    dollar_alpha = attribution.get("dollar_alpha")
    ties = attribution.get("ties_to_headline")
    residual = attribution.get("residual_usd", 0.0)

    tie_note = (
        f"Contributions sum to the headline dollar-alpha "
        f"({_usd_signed(dollar_alpha)}; prior-NAV basis). "
    )
    if ties:
        st.caption(tie_note + "✅ Ties to headline.")
    else:
        st.warning(
            tie_note + f"⚠️ Does NOT tie — residual {_usd_signed(residual)}. "
            "Per-position contributions may be unreliable; investigate the "
            "EOD reconciliation."
        )

    # Horizontal bar of contributions (largest magnitude first)
    ordered = sorted(components, key=lambda c: c.get("contrib_usd", 0.0))
    labels = [c.get("label", "?") for c in ordered]
    values = [c.get("contrib_usd", 0.0) for c in ordered]
    colors = ["#006600" if v >= 0 else "#990000" for v in values]
    fig = go.Figure(
        go.Bar(
            x=values, y=labels, orientation="h", marker_color=colors,
            hovertemplate="%{y}: $%{x:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=max(240, 28 * len(labels) + 80),
        margin=dict(t=10, b=30, l=80, r=20),
        plot_bgcolor="white", paper_bgcolor="white",
        xaxis=dict(title="Contribution to daily alpha ($)", zeroline=True,
                   zerolinecolor="rgba(0,0,0,0.4)", showgrid=True,
                   gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig, use_container_width=True)

    attr_df = pd.DataFrame([
        {
            "Component": c.get("label"),
            "Kind": c.get("kind"),
            "Contribution $": _usd_signed(c.get("contrib_usd")),
            "Contribution (bps)": (
                f"{c.get('contrib_bps'):+.1f}"
                if c.get("contrib_bps") is not None else "—"
            ),
        }
        for c in sorted(components, key=lambda c: c.get("contrib_usd", 0.0))
    ])
    st.dataframe(attr_df, use_container_width=True, hide_index=True)

# --- Open Positions ---------------------------------------------------------
st.header("Open Positions")
positions = report.get("positions", []) or []
if positions:
    pos_df = pd.DataFrame([
        {
            "Ticker": p.get("ticker"),
            "Sector": p.get("sector"),
            "Shares": p.get("shares"),
            "Mkt Value": _usd(p.get("market_value")),
            "% NAV": _pct(p.get("pct_nav"), 1) if p.get("pct_nav") is not None else "—",
            "Day Ret %": _pct(p.get("daily_return_pct")),
            "Day Ret $": _usd_signed(p.get("daily_return_usd")),
            "α contrib (bps)": (
                f"{p.get('alpha_contrib_bps'):+.1f}"
                if p.get("alpha_contrib_bps") is not None else "—"
            ),
        }
        for p in positions
    ])
    st.dataframe(pos_df, use_container_width=True, hide_index=True)
    st.caption(
        "**α contrib (bps)** is each position's additive contribution to the "
        "day's alpha (prior-NAV basis) — it sums, with cash & rotation and "
        "unattributed, to the headline. A position that beat SPY is always "
        "positive, regardless of the portfolio's total sign."
    )

    # --- Position Rationale ---
    rationales = [(p.get("ticker"), p.get("rationale")) for p in positions if p.get("rationale")]
    if rationales:
        st.subheader("Position Rationale")
        for ticker, rationale in rationales:
            with st.expander(ticker):
                st.write(rationale)
else:
    st.info("No open positions in this report.")

# --- Sector Attribution -----------------------------------------------------
sectors = report.get("sector_attribution", []) or []
if sectors:
    st.header("Sector Attribution")
    sec_df = pd.DataFrame([
        {
            "Sector": s.get("sector"),
            "Weight": _pct(s.get("weight_pct"), 1),
            "Contribution": _pct(s.get("contribution_pct")),
            "Positions": s.get("positions"),
        }
        for s in sectors
    ])
    st.dataframe(sec_df, use_container_width=True, hide_index=True)

# --- Trades Today -----------------------------------------------------------
trades = report.get("trades_today", []) or []
if trades:
    st.header("Trades Today")
    tr_df = pd.DataFrame([
        {
            "Action": t.get("action"),
            "Ticker": t.get("ticker"),
            "Shares": t.get("shares"),
            "Price": _usd(t.get("price"), 2),
        }
        for t in trades
    ])
    st.dataframe(tr_df, use_container_width=True, hide_index=True)

# --- Roundtrip Performance --------------------------------------------------
rt = report.get("roundtrip_stats")
if rt and rt.get("n_roundtrips"):
    st.header("Roundtrip Performance (All Time)")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("Closed Roundtrips", rt.get("n_roundtrips"))
    r2.metric("Avg Return", _pct(rt.get("avg_return_pct")))
    r3.metric("Avg Alpha vs SPY", _pct(rt.get("avg_alpha_pct")))
    win = rt.get("win_rate_vs_spy")
    r4.metric("Win Rate vs SPY", f"{win:.0f}%" if win is not None else "—")
    hold = rt.get("avg_hold_days")
    r5.metric("Avg Hold (days)", f"{hold}" if hold is not None else "—")

# --- Trailing History -------------------------------------------------------
history = report.get("trailing_history", []) or []
if history:
    st.header("Trailing History")
    hist_df = pd.DataFrame([
        {
            "Date": h.get("date"),
            "NAV": _usd(h.get("nav")),
            "Return": _pct(h.get("daily_return_pct")),
            "SPY": _pct(h.get("spy_return_pct")),
            "Alpha": _pct(h.get("daily_alpha_pct")),
        }
        for h in history
    ])
    st.dataframe(hist_df, use_container_width=True, hide_index=True)

gen = report.get("generated_at")
if gen:
    st.caption(f"Artifact generated_at: {gen} · schema v{report.get('schema_version', '?')}")
