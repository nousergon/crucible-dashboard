"""
API — Alpha Engine (private console)

All pay-per-token LLM spend — anything billed by the call, as opposed to the
flat Claude Max 20x subscription (see the **Plan** tab for that). Two
independent sources feed this page:

1. **Personal — non-Anthropic** (DeepSeek, etc.) — Brian's own Claude Code
   usage routed through a non-Anthropic provider. Source: the per-(source,date)
   JSON at ``claude_code_usage/{source}/{date}.json``, produced by
   alpha-engine-config ``scripts/collect_usage.py``.
2. **Research pipeline — Anthropic** — per-call Anthropic spend from the
   Saturday research pipeline's agent fleet + intraday alerts. Source:
   ``decision_artifacts/_cost/{date}/cost.parquet``, emitted by
   ``evals/cost_aggregator.py`` on each run.

ROADMAP "Streamlit dashboard cost view (P2)" — gate cleared once the
cost archive crosses ~2 weeks of depth so the weekly trend signal is
meaningful (8 weekly captures as of 2026-05-20, ranging 2026-05-02 to
2026-05-17).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import streamlit as st
from krepis.usage_pacing import reset_window

from loaders.s3_loader import (
    load_claude_code_usage,
    load_llm_cost_parquets,
    load_usage_pacing_config,
)

_PT = ZoneInfo("America/Los_Angeles")
_FALLBACK_WEEKLY_RESET_ANCHOR = datetime(2026, 7, 12, 21, 0)   # PT, naive — Sunday 9pm PT
WEEKLY_PERIOD = timedelta(days=7)

st.divider()

st.title("API")
st.caption(
    "All pay-per-token LLM spend — Brian's personal non-Anthropic usage plus the "
    "research pipeline's Anthropic API spend. See the **Plan** tab for Claude Max "
    "20x subscription (WET-vs-ceiling) tracking, which is separate from all of this."
)

# =============================================================================
# Personal — non-Anthropic (DeepSeek, etc.) API cost
# =============================================================================
st.subheader("Personal — non-Anthropic API cost", divider="orange")

_pacing_cfg = load_usage_pacing_config()
_reset_anchor = (
    datetime.fromisoformat(_pacing_cfg["weekly_reset_anchor_pt"])
    if _pacing_cfg else _FALLBACK_WEEKLY_RESET_ANCHOR
)

df_model, _df_hour = load_claude_code_usage(n_days=35)
df_non_anthropic = df_model[df_model["provider"] == "non-anthropic"] if not df_model.empty else df_model

if df_non_anthropic.empty:
    st.caption("No non-Anthropic usage data yet — routed via Claude Code's provider "
               "config (e.g. DeepSeek). Source: `claude_code_usage/{source}/{date}.json`.")
else:
    now_pt = datetime.now(_PT).replace(tzinfo=None)
    win_start, _next_reset = reset_window(now_pt, _reset_anchor, WEEKLY_PERIOD)
    na_week = df_non_anthropic[df_non_anthropic["date"] >= win_start.date().isoformat()]
    na_week_tokens = int(na_week["total"].sum()) if not na_week.empty else 0
    na_week_cost = float(na_week["cost_usd"].sum()) if not na_week.empty else 0.0

    nc1, nc2, nc3 = st.columns(3)
    nc1.metric("Raw tokens this week", f"{na_week_tokens/1e6:,.0f}M",
               help=f"Reset-aligned window start {win_start:%Y-%m-%d %H:%M} PT "
                    "(same cadence as the Plan tab's Max ceiling reset). "
                    "Non-Anthropic tokens never draw against the Max quota.")
    nc2.metric("API cost this week", f"${na_week_cost:,.2f}",
               help="Notional API-equivalent cost at current public pricing. "
                    "DeepSeek V4-Pro: $1.74/M in, $3.48/M out (non-promo, post-May-31 2026).")
    nc3.metric("Source", f"{na_week['source'].nunique() if not na_week.empty else 0}",
               help="Number of distinct sources contributing non-Anthropic usage.")

    na_m1, na_m2 = st.columns([2, 1])
    na_daily_mod = (df_non_anthropic.groupby(["date", "model"], as_index=False)["total"].sum())
    fig_na_tok = px.bar(na_daily_mod, x="date", y="total", color="model",
                        labels={"total": "raw tokens", "date": ""})
    fig_na_tok.update_layout(barmode="stack", height=300, margin=dict(t=10, b=0, l=0, r=0))
    na_m1.plotly_chart(fig_na_tok, use_container_width=True)

    na_model_cost = (df_non_anthropic.groupby("model", as_index=False)["cost_usd"].sum()
                     .sort_values("cost_usd", ascending=False))
    total_na_cost = na_model_cost["cost_usd"].sum()
    fig_na_pie = px.pie(na_model_cost, names="model", values="cost_usd", hole=0.5)
    fig_na_pie.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0), showlegend=True)
    na_m2.plotly_chart(fig_na_pie, use_container_width=True)
    na_m2.caption(
        f"API-equivalent cost at current public pricing (35-day window). "
        f"Total tracked: **${total_na_cost:,.2f}**. "
        f"DeepSeek V4-Pro: $1.74/M in, $3.48/M out (non-promo, post-May-31 2026)."
    )

    na_daily_cost = (df_non_anthropic.groupby(["date", "model"], as_index=False)["cost_usd"].sum())
    fig_na_cost = px.bar(na_daily_cost, x="date", y="cost_usd", color="model",
                         labels={"cost_usd": "API cost ($)", "date": ""})
    fig_na_cost.update_layout(barmode="stack", height=280, margin=dict(t=10, b=0, l=0, r=0))
    st.plotly_chart(fig_na_cost, use_container_width=True)

st.markdown("---")

# =============================================================================
# Research pipeline — Anthropic API cost
# =============================================================================
st.subheader("Research pipeline — Anthropic API cost", divider="gray")
st.caption(
    "Per-call Anthropic spend from the Saturday research pipeline + intraday "
    "alerts. Source: `decision_artifacts/_cost/{date}/cost.parquet`, emitted "
    "by `evals/cost_aggregator.py` on each run."
)

df = load_llm_cost_parquets(n_recent=12)

if df.empty:
    st.info(
        "No cost captures available yet. The first parquet lands at the end of "
        "the next Saturday research pipeline run via `cost_aggregator.write_run_artifacts`."
    )
    st.stop()

# Normalize timestamp to a tz-naive datetime + derive iso date
df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True).dt.tz_convert(None)
df["call_date"] = df["timestamp"].dt.date.astype(str)

# ─── Headline KPIs ──────────────────────────────────────────────────────────
total_usd = float(df["cost_usd"].fillna(0).sum())
total_calls = int(len(df))
n_dates = df["capture_date"].nunique()
latest_capture = df["capture_date"].max()
latest_df = df[df["capture_date"] == latest_capture]
latest_usd = float(latest_df["cost_usd"].fillna(0).sum())
mean_per_capture = total_usd / n_dates if n_dates else 0.0
delta_vs_mean = latest_usd - mean_per_capture

# Tool-fee KPI — schema v2 (additive). v1 partitions return NaN → 0.
total_web_search = int(df.get("web_search_requests", pd.Series(dtype="int64")).fillna(0).sum())
total_web_fetch = int(df.get("web_fetch_requests", pd.Series(dtype="int64")).fillna(0).sum())

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total spend (window)", f"${total_usd:,.2f}", help=f"Across {n_dates} capture date(s)")
k2.metric("Latest capture", str(latest_capture))
k3.metric("Latest spend", f"${latest_usd:,.2f}", delta=f"{delta_vs_mean:+.2f} vs window mean")
k4.metric("Calls (window)", f"{total_calls:,}")
k5.metric(
    "Server-tool requests",
    f"{total_web_search + total_web_fetch:,}",
    help=(
        f"web_search={total_web_search:,} (≈${total_web_search * 0.01:,.4f}) + "
        f"web_fetch={total_web_fetch:,} (free). Schema v2 column — pre-v2 "
        f"partitions report 0 since the field didn't exist yet."
    ),
)

st.markdown("---")

# ─── Trend ──────────────────────────────────────────────────────────────────
st.subheader("Weekly cost trend")
trend = df.groupby("capture_date", as_index=False)["cost_usd"].sum().sort_values("capture_date")
trend["cost_usd"] = trend["cost_usd"].round(4)

fig_trend = px.line(
    trend,
    x="capture_date",
    y="cost_usd",
    markers=True,
    title=None,
    labels={"capture_date": "Capture date", "cost_usd": "USD"},
)
fig_trend.update_traces(line=dict(width=2))
fig_trend.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
st.plotly_chart(fig_trend, use_container_width=True)

# ─── Breakdowns ─────────────────────────────────────────────────────────────
tab_agent, tab_team, tab_model, tab_prompt, tab_calls = st.tabs(
    ["By agent", "By sector team", "By model", "By prompt version", "Recent calls"]
)


def _agg_with_tool_requests_on(frame: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """Group + sum cost, calls, tokens, AND server-tool requests on *frame*.

    Server-tool columns (``web_search_requests`` + ``web_fetch_requests``)
    are schema v2 additive — present on partitions written after
    alpha-engine-research #232 / alpha-engine #210 / alpha-engine-data
    #308 (all 2026-05-25). Pre-v2 partitions have NaN; the .fillna(0)
    on the agg pre-stage keeps the sum well-defined across mixed-vintage
    windows.
    """
    df_with_tool_zeros = frame.copy()
    for col in ("web_search_requests", "web_fetch_requests"):
        if col not in df_with_tool_zeros.columns:
            df_with_tool_zeros[col] = 0
        df_with_tool_zeros[col] = df_with_tool_zeros[col].fillna(0)
    return (
        df_with_tool_zeros.groupby(group_col, as_index=False)
        .agg(
            cost_usd=("cost_usd", "sum"),
            calls=("cost_usd", "size"),
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            web_search_requests=("web_search_requests", "sum"),
            web_fetch_requests=("web_fetch_requests", "sum"),
        )
        .sort_values("cost_usd", ascending=False)
    )


def _agg_with_tool_requests(group_col: str) -> pd.DataFrame:
    """Window-wide convenience wrapper — groups the module-level ``df``."""
    return _agg_with_tool_requests_on(df, group_col)


with tab_agent:
    agent_df = _agg_with_tool_requests("agent_id")
    agent_df["cost_usd"] = agent_df["cost_usd"].round(4)
    fig_agent = px.bar(
        agent_df,
        x="agent_id",
        y="cost_usd",
        labels={"agent_id": "Agent", "cost_usd": "USD"},
    )
    fig_agent.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig_agent, use_container_width=True)
    st.dataframe(agent_df, use_container_width=True, hide_index=True)

with tab_team:
    # ROADMAP L1141 deliverable (c) — cost-by-sector-team-by-week heatmap +
    # drilldown table. Cross-sector agents (macro_economist, ic_cio) have
    # sector_team_id=None by design; we surface them under "(none)" to keep
    # them visible rather than dropped. Aggregator uses the same convention
    # (scripts/aggregate_costs._group_sum at the research side).
    if "sector_team_id" not in df.columns:
        st.info(
            "No `sector_team_id` data in the window — pre-schema cost archives "
            "didn't stamp the team dimension. The field went live with the "
            "Saturday research pipeline cost-telemetry rollout; new captures "
            "carry it automatically."
        )
    else:
        team_df_raw = df.copy()
        team_df_raw["sector_team_id"] = team_df_raw["sector_team_id"].fillna("(none)").astype(str)

        # Drilldown table — aggregate over the full window.
        team_df = _agg_with_tool_requests_on(team_df_raw, "sector_team_id")
        team_df["cost_usd"] = team_df["cost_usd"].round(4)

        # Heatmap — sector_team rows × capture_date columns × cost_usd cells.
        # Pivot then sort rows by total cost desc so the heaviest spenders
        # land at the top; capture_date columns are time-ordered so the
        # cross-section reads left-to-right as a trend.
        pivot = (
            team_df_raw.groupby(["sector_team_id", "capture_date"], as_index=False)["cost_usd"]
            .sum()
            .pivot(index="sector_team_id", columns="capture_date", values="cost_usd")
            .fillna(0.0)
        )
        # Order rows by total spend descending — heaviest at top.
        row_totals = pivot.sum(axis=1).sort_values(ascending=False)
        pivot = pivot.loc[row_totals.index]
        # Order columns by capture_date ascending — left-to-right trend.
        pivot = pivot[sorted(pivot.columns)]

        fig_team_heat = px.imshow(
            pivot.values,
            x=list(pivot.columns),
            y=list(pivot.index),
            color_continuous_scale="Blues",
            labels=dict(x="Capture date", y="Sector team", color="USD"),
            aspect="auto",
            text_auto=".2f",
        )
        fig_team_heat.update_layout(
            height=max(280, 40 * len(pivot.index) + 80),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig_team_heat, use_container_width=True)
        st.caption(
            "Heatmap cell = total Anthropic spend (USD) for that sector team on "
            "that capture date. Cross-sector agents (`macro_economist`, "
            "`ic_cio`) carry `sector_team_id=None` upstream and surface here "
            "under `(none)`."
        )
        st.dataframe(team_df, use_container_width=True, hide_index=True)

with tab_model:
    model_df = _agg_with_tool_requests("model_name")
    model_df["cost_usd"] = model_df["cost_usd"].round(4)
    fig_model = px.bar(
        model_df,
        x="model_name",
        y="cost_usd",
        labels={"model_name": "Model", "cost_usd": "USD"},
    )
    fig_model.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig_model, use_container_width=True)
    st.dataframe(model_df, use_container_width=True, hide_index=True)

with tab_prompt:
    if "prompt_version" not in df.columns or df["prompt_version"].dropna().empty:
        st.info(
            "No `prompt_version` data in the window. Per-prompt-version "
            "attribution requires the cost-telemetry stream to stamp "
            "`prompt_id` + `prompt_version` on each call (already wired in "
            "research's `track_llm_cost`; data + executor raw-SDK paths "
            "use `record_anthropic_call` which doesn't currently plumb "
            "prompt-version metadata)."
        )
    else:
        # Show prompt_id alongside version so identically-named prompts
        # under different ids stay distinguishable.
        version_df = df.copy()
        version_df["prompt_label"] = (
            version_df.get("prompt_id", pd.Series(dtype=object)).fillna("(unknown)")
            .astype(str) + " @ "
            + version_df["prompt_version"].fillna("(none)").astype(str)
        )
        prompt_df = (
            version_df.groupby("prompt_label", as_index=False)
            .agg(
                cost_usd=("cost_usd", "sum"),
                calls=("cost_usd", "size"),
                input_tokens=("input_tokens", "sum"),
                output_tokens=("output_tokens", "sum"),
            )
            .sort_values("cost_usd", ascending=False)
        )
        prompt_df["cost_usd"] = prompt_df["cost_usd"].round(4)
        # Top 20 prevents the chart from getting unreadable on
        # high-cardinality prompt sets; the table below shows the full list.
        fig_prompt = px.bar(
            prompt_df.head(20),
            x="prompt_label",
            y="cost_usd",
            labels={"prompt_label": "Prompt id @ version", "cost_usd": "USD"},
        )
        fig_prompt.update_layout(
            height=360, margin=dict(l=0, r=0, t=10, b=0),
            xaxis_tickangle=-30,
        )
        st.plotly_chart(fig_prompt, use_container_width=True)
        st.caption(
            "Catches prompt edits that inflate cost: a new `prompt_version` "
            "row appearing with materially higher per-call cost signals a "
            "regression worth investigating."
        )
        st.dataframe(prompt_df, use_container_width=True, hide_index=True)

with tab_calls:
    recent = (
        latest_df[
            [
                "timestamp",
                "agent_id",
                "node_name",
                "model_name",
                "input_tokens",
                "output_tokens",
                "cost_usd",
            ]
        ]
        .sort_values("cost_usd", ascending=False)
        .head(25)
    )
    st.caption(f"Top 25 calls by cost from the latest capture ({latest_capture}, {len(latest_df)} total calls).")
    st.dataframe(recent, use_container_width=True, hide_index=True)

st.markdown("---")
st.caption(
    "Research-fleet window covers the most recent 12 capture dates. Production cost-cap "
    "thresholds live in the Saturday pipeline's hard ceiling check; this surface is "
    "observation-only."
)
