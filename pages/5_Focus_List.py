"""Focus List page — Is the scanner picking the names the agent eventually picks?

Surfaces the regime-blended factor-composite focus list and its divergence
from the quant agent's actual picks. The audit answers four questions:

  • Precision           — of focus-list names, how many did the agent pick?
                          (was the focus list a good predictor of picks?)
  • Recall              — of agent picks, how many were in the focus list?
                          (did the focus list cover the agent's picks?)
  • Override hit rate   — when the agent reached outside the focus list via
                          @tool get_factor_profile, did those picks land?
  • Stance distribution — does BULL regime surface mostly momentum stances,
                          and BEAR mostly low_vol/quality? Mismatches flag
                          blend-weight miscalibration.

Plan doc: alpha-engine-docs/private/scanner-260514.md §5.3.

First data: Saturday 2026-05-17 SF onward (post-research #183 shadow audit
wiring). Renders gracefully when no data is available yet.
"""

import os
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.db_loader import (
    get_focus_list_audit,
    get_focus_list_stance_mix,
    get_focus_list_weekly_summary,
)


st.set_page_config(page_title="Focus List — Alpha Engine", layout="wide")
st.title("Focus List")
st.caption(
    "Regime-blended factor-composite focus list vs the quant agent's actual "
    "picks. Audit is observability — focus-list gating is a separate flag "
    "(`FOCUS_LIST_GATING_ENABLED`)."
)


# ---------------------------------------------------------------------------
# Load + empty-state
# ---------------------------------------------------------------------------

weekly = get_focus_list_weekly_summary()
audit = get_focus_list_audit()
stance = get_focus_list_stance_mix()

if weekly.empty and audit.empty:
    st.info(
        "**Focus list audit data not available yet.** The shadow audit columns "
        "(`focus_score`, `focus_list_passed`, `agent_override`) were added in "
        "alpha-engine-research #183 (schema v17). First data populates on the "
        "Saturday SF run after that migration applies — expected from "
        "**2026-05-17** onward. Until then, this page is a placeholder."
    )
    st.stop()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_summary, tab_trend, tab_stance, tab_rows = st.tabs([
    "Summary",
    "Trend",
    "Stance",
    "Rows",
])


# ── Summary tab ──────────────────────────────────────────────────────────────


with tab_summary:
    st.subheader("Latest week per team")
    if weekly.empty:
        st.warning("No focus_list_by_team aggregates yet.")
    else:
        # Take most recent eval_date
        latest_date = weekly["eval_date"].max()
        latest = weekly[weekly["eval_date"] == latest_date].copy()
        st.caption(f"Run date: **{latest_date}**")

        # Pretty columns
        display = latest[[
            "focus_team_id", "n_focus_list", "n_picks", "n_overrides",
            "precision", "recall", "override_hit_rate",
        ]].rename(columns={
            "focus_team_id": "team",
            "n_focus_list": "focus_list_size",
            "n_picks": "agent_picks",
            "n_overrides": "tool_overrides",
        })
        # Format rates as percentages
        for col in ("precision", "recall", "override_hit_rate"):
            display[col] = display[col].apply(
                lambda v: f"{100*v:.0f}%" if pd.notna(v) else "—"
            )
        st.dataframe(display, use_container_width=True, hide_index=True)

        # Headline metrics across all teams for the latest week
        st.subheader("Aggregate across teams (latest week)")
        col1, col2, col3, col4 = st.columns(4)
        n_fl = int(latest["n_focus_list"].sum())
        n_pk = int(latest["n_picks"].sum())
        n_ov = int(latest["n_overrides"].sum())
        # Weighted aggregates: use raw counts to compute, not mean of rates
        agg_precision = (
            latest["n_focus_and_picked"].sum() / n_fl if n_fl else None
        )
        col1.metric("Focus list total", n_fl)
        col2.metric("Agent picks total", n_pk)
        col3.metric("Tool overrides", n_ov)
        col4.metric(
            "Aggregate precision",
            f"{100*agg_precision:.0f}%" if agg_precision is not None else "—",
        )


# ── Trend tab ────────────────────────────────────────────────────────────────


with tab_trend:
    st.subheader("Precision / recall over time")
    if weekly.empty:
        st.warning("Need at least 2 weekly runs for a trend.")
    else:
        # Long format for plotly facet
        long = weekly.melt(
            id_vars=["eval_date", "focus_team_id"],
            value_vars=["precision", "recall", "override_hit_rate"],
            var_name="metric",
            value_name="rate",
        ).dropna(subset=["rate"])
        if long.empty:
            st.info("No rates computable yet (all zero denominators).")
        else:
            fig = px.line(
                long,
                x="eval_date",
                y="rate",
                color="focus_team_id",
                facet_col="metric",
                markers=True,
                labels={"rate": "rate (0-1)", "eval_date": "run date"},
                title="Focus list metrics per team, per week",
            )
            fig.update_yaxes(range=[0, 1], matches=None)
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("Funnel cardinality over time")
    if not weekly.empty:
        fig2 = px.bar(
            weekly,
            x="eval_date",
            y=["n_focus_list", "n_picks", "n_overrides"],
            color_discrete_sequence=["#4c78a8", "#f58518", "#e45756"],
            barmode="group",
            facet_col="focus_team_id",
            facet_col_wrap=3,
            labels={"value": "count", "eval_date": "run date"},
            title="Focus list / picks / overrides per team, per week",
        )
        fig2.update_layout(height=520, showlegend=True)
        st.plotly_chart(fig2, use_container_width=True)


# ── Stance tab ───────────────────────────────────────────────────────────────


with tab_stance:
    st.subheader("Latest focus-list stance distribution per team")
    st.caption(
        "Dominant factor (momentum / quality / value / low_vol) per focus-list "
        "ticker. BULL regime should surface mostly momentum + quality; BEAR "
        "should surface mostly low_vol + quality. Mismatches flag blend-weight "
        "miscalibration."
    )
    if stance.empty:
        st.warning("No stance data available yet.")
    else:
        fig3 = px.bar(
            stance,
            x="focus_team_id",
            y="n",
            color="focus_stance",
            barmode="stack",
            labels={"n": "count", "focus_team_id": "team"},
            title="Focus list stance mix (latest week)",
        )
        fig3.update_layout(height=420)
        st.plotly_chart(fig3, use_container_width=True)


# ── Rows tab ─────────────────────────────────────────────────────────────────


with tab_rows:
    st.subheader("Per-ticker audit rows (latest week)")
    if audit.empty:
        st.warning("No audit rows.")
    else:
        latest_date = audit["eval_date"].max()
        recent = audit[audit["eval_date"] == latest_date].copy()
        st.caption(f"Run date: **{latest_date}** — {len(recent)} rows")

        # Filters
        with st.expander("Filters", expanded=False):
            teams = sorted(t for t in recent["focus_team_id"].dropna().unique())
            team_filter = st.multiselect("Team", options=teams, default=teams)
            stances = sorted(
                s for s in recent["focus_stance"].dropna().unique()
            )
            stance_filter = st.multiselect(
                "Stance", options=stances, default=stances,
            )
            only_overrides = st.checkbox(
                "Only agent overrides", value=False,
            )
            only_passed = st.checkbox(
                "Only focus list members", value=False,
            )

        filtered = recent
        if team_filter:
            filtered = filtered[
                filtered["focus_team_id"].isin(team_filter)
                | filtered["focus_team_id"].isna()
            ]
        if stance_filter:
            filtered = filtered[
                filtered["focus_stance"].isin(stance_filter)
                | filtered["focus_stance"].isna()
            ]
        if only_overrides:
            filtered = filtered[filtered["agent_override"] == 1]
        if only_passed:
            filtered = filtered[filtered["focus_list_passed"] == 1]

        st.dataframe(
            filtered.sort_values(
                ["focus_team_id", "focus_rank_in_team"],
                na_position="last",
            ),
            use_container_width=True,
            hide_index=True,
        )
