"""
LLM Cost — Alpha Engine (private console)

Per-Saturday and trailing-trend visibility on Anthropic LLM spend across
the Research pipeline's agent fleet. Reads the per-call cost archive
written by alpha-engine-research's `evals/cost_aggregator.py`
(`decision_artifacts/_cost/{date}/cost.parquet`, ~weekly cadence).

ROADMAP "Streamlit dashboard cost view (P2)" — gate cleared once the
cost archive crosses ~2 weeks of depth so the weekly trend signal is
meaningful (8 weekly captures as of 2026-05-20, ranging 2026-05-02 to
2026-05-17).
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import streamlit as st

from loaders.s3_loader import load_llm_cost_parquets

st.set_page_config(
    page_title="LLM Cost — Alpha Engine",
    page_icon="💸",
    layout="wide",
)

st.divider()

st.title("LLM Cost")
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
tab_agent, tab_model, tab_prompt, tab_calls = st.tabs(
    ["By agent", "By model", "By prompt version", "Recent calls"]
)


def _agg_with_tool_requests(group_col: str) -> pd.DataFrame:
    """Group + sum cost, calls, tokens, AND server-tool requests.

    Server-tool columns (``web_search_requests`` + ``web_fetch_requests``)
    are schema v2 additive — present on partitions written after
    alpha-engine-research #232 / alpha-engine #210 / alpha-engine-data
    #308 (all 2026-05-25). Pre-v2 partitions have NaN; the .fillna(0)
    on the agg pre-stage keeps the sum well-defined across mixed-vintage
    windows.
    """
    df_with_tool_zeros = df.copy()
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
    "Window covers the most recent 12 capture dates. Production cost-cap thresholds live in the "
    "Saturday pipeline's hard ceiling check; this surface is observation-only."
)

