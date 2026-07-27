"""Universe Churn — Alpha Engine (private console)

**Who stays in the cut, and what does the churn look like?**

Every other universe surface is a single snapshot: the Board ranks one ``as_of``,
the Funnel shows one week's ~900→60 cut, Trends plots one ticker at a time. None
can show that a name has held the top 60 for nine straight weeks, or that two
thirds of the scanner cut turns over every Saturday.

Reads the versioned cut contract crucible-research publishes each cycle
(``universe_membership/{date}/membership.json``, ``scoring/universe_membership.py``).
No LLM call, no cost, no metric computed here that isn't set arithmetic over that
artifact.

The three bases are kept SEPARATE on purpose — their divergence is the finding.
Over 2026-05-29 → 2026-07-24 the attractiveness top-60 retains ~78% of its members
week over week while the scanner cut retains 22-47%: the ranking is stable, and
the gate is what churns. A view that averaged them would hide exactly that.

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit chrome
— no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import load_universe_membership_history
from loaders.universe_churn import (
    backfilled_dates,
    churn_table,
    cut_comparison,
    cut_label,
    cut_names,
    membership_matrix,
    membership_series,
    survivors,
    tenure_distribution,
    tenure_table,
)

# Categorical hues, assigned to cuts in FIXED order (never cycled). Validated for
# colorblind separation and surface contrast in BOTH light and dark modes — the
# console's theme is the viewer's choice, so a palette that only passes one mode
# would be a coin flip. Identity is never carried by color alone: every chart
# below also names its series in a legend or on the axis.
_HUES = ["#4590e2", "#c47a22", "#25966d"]
# Composition steps (parts of one whole — retained vs new), one hue light→dark.
# Deliberately NOT two categorical hues: these are portions of a total, not
# independent identities.
_RETAINED = "#25966d"
_NEW = "#9ed9c1"
_GRID = "rgba(128,128,128,0.18)"

st.markdown("### 🔁 Universe Churn")
st.caption(
    "Cut membership over time — who persists, who rotates, and how fast. Read "
    "from the weekly `universe_membership` artifact (no LLM call, no cost)."
)

memberships = load_universe_membership_history()

if not memberships:
    st.warning(
        "No universe membership artifacts yet. They are produced each weekly "
        "cycle by crucible-research's Scanner "
        "(`scoring/universe_membership.py` → `universe_membership/{date}/"
        "membership.json`). Historical cycles can be reconstructed with that "
        "repo's `scripts/backfill_universe_membership.py`."
    )
    st.stop()

available_cuts = cut_names(memberships)
backfilled = backfilled_dates(memberships)

# ── Controls ─────────────────────────────────────────────────────────────────
c1, c2 = st.columns([2, 1])
with c1:
    selected_cuts = st.multiselect(
        "Cuts to compare",
        available_cuts,
        default=available_cuts[:3],
        format_func=cut_label,
        key="churn_cuts",
    )
with c2:
    focus = st.selectbox(
        "Detail cut",
        available_cuts,
        index=0,
        format_func=cut_label,
        key="churn_focus",
    )

if not selected_cuts:
    st.info("Select at least one cut to compare.")
    st.stop()

# ── Headline ─────────────────────────────────────────────────────────────────
cycles = sorted({m.get("run_date") for m in memberships if m.get("run_date")})
m1, m2, m3, m4 = st.columns(4)
m1.metric("Cycles recorded", len(cycles))
m2.metric("First cycle", cycles[0] if cycles else "—")
m3.metric("Latest cycle", cycles[-1] if cycles else "—")
focus_churn = churn_table(memberships, focus)
_latest_retention = focus_churn["retention_pct"].dropna()
m4.metric(
    f"{cut_label(focus)} retention",
    f"{_latest_retention.iloc[-1]:.0f}%" if not _latest_retention.empty else "—",
    help="Share of last cycle's members still in the cut this cycle.",
)

if backfilled:
    st.caption(
        f"⚠️ {len(backfilled)} of {len(cycles)} cycles were RECONSTRUCTED from "
        f"historical artifacts rather than emitted live ({backfilled[0]} → "
        f"{backfilled[-1]}). A reconstruction is only as good as the artifacts "
        "that survived — treat those points as slightly weaker evidence."
    )

st.divider()

# ── 1. Cut comparison ────────────────────────────────────────────────────────
st.markdown("#### 📊 Cuts side by side")
comparison = cut_comparison(memberships, selected_cuts)
st.dataframe(
    comparison, use_container_width=True, hide_index=True,
    column_config={
        "cut": st.column_config.TextColumn("Cut"),
        "cycles": st.column_config.NumberColumn("Cycles", format="%d"),
        "mean_size": st.column_config.NumberColumn("Mean size", format="%.0f"),
        "mean_retention_pct": st.column_config.NumberColumn(
            "Mean retention", format="%.0f%%",
            help="Average share of the prior cycle's members retained.",
        ),
        "survivors": st.column_config.NumberColumn(
            "Survivors", format="%d",
            help="Names present in EVERY recorded cycle.",
        ),
    },
)
st.caption(
    "A low-retention cut with zero survivors is rotating through names; a "
    "high-retention cut with a persistent core is stable. Compare the scanner "
    "gate against the attractiveness ranking — they are count-matched at 60."
)

# ── 2. Retention over time ───────────────────────────────────────────────────
st.markdown("#### 📈 Week-over-week retention")
fig = go.Figure()
for i, cut in enumerate(selected_cuts):
    table = churn_table(memberships, cut)
    if table.empty:
        continue
    plotted = table.dropna(subset=["retention_pct"])
    if plotted.empty:
        continue
    fig.add_trace(go.Scatter(
        x=plotted["as_of"], y=plotted["retention_pct"],
        mode="lines+markers", name=cut_label(cut),
        line=dict(color=_HUES[i % len(_HUES)], width=2),
        marker=dict(size=8),
        hovertemplate="%{x}<br>%{y:.0f}% retained<extra>" + cut_label(cut) + "</extra>",
    ))
fig.update_layout(
    height=320, margin=dict(l=10, r=10, t=10, b=10),
    yaxis=dict(title="% of prior cycle retained", range=[0, 100], gridcolor=_GRID),
    xaxis=dict(title=None, gridcolor=_GRID),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
    hovermode="x unified", plot_bgcolor="rgba(0,0,0,0)",
)
st.plotly_chart(fig, use_container_width=True)
st.caption(
    "The first cycle has no predecessor and is omitted — it has nothing to be "
    "retained from."
)

st.divider()

# ── 3. Churn composition for the focus cut ───────────────────────────────────
st.markdown(f"#### 🔀 {cut_label(focus)} — retained vs new, per cycle")
composition = focus_churn.dropna(subset=["retained"])
if composition.empty:
    st.caption("Needs at least two recorded cycles to show composition.")
else:
    bars = go.Figure()
    bars.add_trace(go.Bar(
        x=composition["as_of"], y=composition["retained"], name="Retained",
        marker=dict(color=_RETAINED, line=dict(width=0)),
        hovertemplate="%{x}<br>%{y} retained<extra></extra>",
    ))
    bars.add_trace(go.Bar(
        x=composition["as_of"], y=composition["new"], name="New",
        marker=dict(color=_NEW, line=dict(width=0)),
        hovertemplate="%{x}<br>%{y} new<extra></extra>",
    ))
    bars.update_layout(
        barmode="stack", bargap=0.35, height=300,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(title="Names in cut", gridcolor=_GRID),
        xaxis=dict(title=None, gridcolor=_GRID),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(bars, use_container_width=True)
    st.caption(
        "Stack height is the cut size; the pale segment is how much of it is "
        "brand new that week."
    )

# ── 4. Membership heatmap ────────────────────────────────────────────────────
st.markdown(f"#### 🗓 {cut_label(focus)} — who was in, week by week")
_HEATMAP_ROWS = 40
matrix = membership_matrix(memberships, focus, top_n=_HEATMAP_ROWS)
tenure = tenure_table(memberships, focus)
if matrix.empty:
    st.caption("No membership recorded for this cut.")
else:
    heat = go.Figure(go.Heatmap(
        z=matrix.values, x=list(matrix.columns), y=list(matrix.index),
        colorscale=[[0, "rgba(128,128,128,0.10)"], [1, _HUES[0]]],
        showscale=False, xgap=2, ygap=2,
        hovertemplate="%{y} — %{x}<extra></extra>",
    ))
    heat.update_layout(
        height=max(320, 18 * len(matrix.index)),
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(side="top", tickangle=-45),
        yaxis=dict(autorange="reversed"),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(heat, use_container_width=True)
    total_names = len(tenure)
    if total_names > _HEATMAP_ROWS:
        st.caption(
            f"Showing the {_HEATMAP_ROWS} longest-tenured of {total_names} "
            f"distinct names ever in this cut — the remaining "
            f"{total_names - _HEATMAP_ROWS} appear in fewer cycles. Full list "
            "in the tenure table below."
        )

# ── 5. Survivors + tenure ────────────────────────────────────────────────────
persistent = survivors(memberships, focus)
n_cycles = len(membership_series(memberships, focus))
if persistent:
    st.success(
        f"**{len(persistent)} names in {cut_label(focus)} every one of "
        f"{n_cycles} cycles:** {', '.join(persistent)}"
    )
else:
    st.info(
        f"**No name held {cut_label(focus)} across all {n_cycles} recorded "
        "cycles.** For the scanner gate cut this is the expected result, and "
        "the headline finding: the cut is a weekly re-draw, not a watchlist."
    )

st.markdown("#### ⏳ Tenure")
t1, t2 = st.columns([1, 2])
with t1:
    dist = tenure_distribution(memberships, focus)
    if not dist.empty:
        dist_fig = go.Figure(go.Bar(
            x=dist["weeks_in"], y=dist["tickers"],
            marker=dict(color=_HUES[0], line=dict(width=0)),
            hovertemplate="%{x} cycles → %{y} names<extra></extra>",
        ))
        dist_fig.update_layout(
            height=280, bargap=0.3, margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(title="Cycles in cut", dtick=1, gridcolor=_GRID),
            yaxis=dict(title="Distinct names", gridcolor=_GRID),
            plot_bgcolor="rgba(0,0,0,0)", showlegend=False,
        )
        st.plotly_chart(dist_fig, use_container_width=True)
        st.caption("Mass at 1 = churn; mass at the right = a stable core.")
with t2:
    st.dataframe(
        tenure, use_container_width=True, hide_index=True, height=280,
        column_config={
            "ticker": st.column_config.TextColumn("Ticker"),
            "weeks_in": st.column_config.NumberColumn("Cycles in", format="%d"),
            "current_streak": st.column_config.NumberColumn(
                "Streak", format="%d",
                help="Consecutive cycles in the cut, counted back from the latest.",
            ),
            "in_latest": st.column_config.CheckboxColumn("In latest"),
            "first_seen": st.column_config.TextColumn("First"),
            "last_seen": st.column_config.TextColumn("Last"),
        },
    )

# ── Per-cycle detail (the table view behind every chart above) ───────────────
with st.expander("Per-cycle churn detail"):
    st.dataframe(
        focus_churn, use_container_width=True, hide_index=True,
        column_config={
            "as_of": st.column_config.TextColumn("Cycle"),
            "size": st.column_config.NumberColumn("Size", format="%d"),
            "retained": st.column_config.NumberColumn("Retained", format="%d"),
            "new": st.column_config.NumberColumn("New", format="%d"),
            "dropped": st.column_config.NumberColumn("Dropped", format="%d"),
            "retention_pct": st.column_config.NumberColumn(
                "Retention", format="%.0f%%"
            ),
        },
    )

st.caption(
    "Membership lists in the artifact are sorted, not rank-ordered, so this "
    "page compares MEMBERSHIP rather than position — a name sliding from rank 3 "
    "to rank 40 stays 'retained' and is not counted as churn."
)
