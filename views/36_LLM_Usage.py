"""
Plan — Alpha Engine (private console)

How much of Brian's Claude **Max 20x** weekly quota is being consumed.
Tracked in **WET** (weighted effective tokens — Opus-input-equivalent,
price-independent) against a weekly reset cycle. Anthropic publishes no
exact Max 20x limit, so the ceiling is an empirically-calibrated constant —
see the "% of weekly ceiling" gauge below.

Non-Anthropic (DeepSeek, etc.) spend does NOT draw against this quota — it's
billed per-token instead, and lives on the **API** tab alongside the
research pipeline's Anthropic API spend.

Source: the per-(source,date) JSON at ``claude_code_usage/{source}/{date}.json``,
produced by alpha-engine-config ``scripts/collect_usage.py`` (hourly launchd on
the laptop; run-scoped ``source='groom'`` from the GHA groom; run-scoped
``source='watch'`` from the Fleet-SF/CI Watch agent runs — config#1899).

The "% of weekly ceiling" gauge uses an empirically-calibrated constant, read
from the SSoT ``config/usage_pacing.json`` (config#2043) — recalibrate via
alpha-engine-config's ``scripts/set_usage_pacing_config.py``, cross-referencing
``/usage``. Falls back to a hardcoded constant below if that S3 object is
unavailable.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import streamlit as st
from krepis.usage_pacing import reset_window

from loaders.s3_loader import load_claude_code_usage, load_usage_pacing_config

# SSoT (config#2043): s3://alpha-engine-research/config/usage_pacing.json,
# written by alpha-engine-config's scripts/set_usage_pacing_config.py and also
# read by that repo's groom_budget.py + the alpha-engine-data usage-pace-alert
# Lambda, so all three consumers stay bit-for-bit in sync. The constants below
# are FALLBACK ONLY (used iff the S3 object is missing/unparseable).
_FALLBACK_WEEKLY_WET_CEILING = 850_000_000
_PT = ZoneInfo("America/Los_Angeles")
_FALLBACK_WEEKLY_RESET_ANCHOR = datetime(2026, 7, 12, 21, 0)   # PT, naive — Sunday 9pm PT
WEEKLY_PERIOD = timedelta(days=7)

_pacing_cfg = load_usage_pacing_config()
if _pacing_cfg:
    WEEKLY_WET_CEILING = float(_pacing_cfg["weekly_wet_ceiling"])
    WEEKLY_RESET_ANCHOR = datetime.fromisoformat(_pacing_cfg["weekly_reset_anchor_pt"])
    _ceiling_source = f"SSoT, calibrated {_pacing_cfg.get('calibrated_date', '?')}"
else:
    WEEKLY_WET_CEILING = _FALLBACK_WEEKLY_WET_CEILING
    WEEKLY_RESET_ANCHOR = _FALLBACK_WEEKLY_RESET_ANCHOR
    _ceiling_source = "fallback constant — config/usage_pacing.json unavailable"


def _model_since(df_model: pd.DataFrame, start: datetime) -> pd.DataFrame:
    """Rows at/after ``start`` (day-granularity; cache fields live on df_model)."""
    return df_model.loc[df_model["date"] >= start.date().isoformat()]


st.divider()
st.title("Plan")
st.caption(
    "Brian's Claude Max 20x weekly quota consumption, tracked in **WET** "
    "(weighted effective tokens — price-independent) against the weekly reset. "
    "Non-Anthropic spend (DeepSeek, etc.) does not draw against this quota — "
    "see the **API** tab for that plus the research pipeline's Anthropic spend."
)

df_model, df_hour = load_claude_code_usage(n_days=35)

if df_model.empty:
    st.info(
        "No usage data yet. Install the hourly producer on the laptop:\n\n"
        "`bash scripts/install_usage_launchd.sh`  (in alpha-engine-config)\n\n"
        "It writes `claude_code_usage/{source}/{date}.json` to S3; this page reads it."
    )
    st.stop()

# --- provider split (Max ceiling only counts Anthropic) -----------------------
df_anthropic = df_model[df_model["provider"] == "anthropic"]

now_pt = datetime.now(_PT).replace(tzinfo=None)
win_start, next_reset = reset_window(now_pt, WEEKLY_RESET_ANCHOR, WEEKLY_PERIOD)

# Anthropic metrics (WET toward Max ceiling) — day-granularity, Anthropic-only.
# (Hour-granularity df_hour sums WET across ALL providers with no provider
# breakdown, so it can't be used for the ceiling gauge without pulling in
# non-Anthropic WET; day-granularity df_anthropic is already provider-filtered.)
ant_week_wet = float(df_anthropic.loc[df_anthropic["date"] >= win_start.date().isoformat(), "wet"].sum())
all_week_wet = df_model[df_model["date"] >= win_start.date().isoformat()]["wet"].sum()
roll = df_model[df_model["date"] >= (now_pt.date() - timedelta(days=6)).isoformat()]["wet"].sum()
pct = (ant_week_wet / WEEKLY_WET_CEILING) if WEEKLY_WET_CEILING else 0.0
hrs_to_reset = max(0, (next_reset - now_pt).total_seconds()) / 3600.0

# =============================================================================
# HEADLINE: Anthropic (Max ceiling)
# =============================================================================
st.subheader("Anthropic — Max 20x ceiling", divider="gray")
ac1, ac2, ac3 = st.columns(3)
ac1.metric("This week's WET (Anthropic only)", f"{ant_week_wet/1e6:,.0f}M",
           help=f"Reset-aligned window start {win_start:%Y-%m-%d %H:%M} PT. "
                f"All-provider WET (incl. non-Anthropic, informational only): "
                f"{all_week_wet/1e6:,.0f}M. Rolling-7d: {roll/1e6:,.0f}M.")
ac2.metric("% of weekly ceiling", f"{pct*100:,.0f}%",
           help=f"This week's Anthropic-only WET / {WEEKLY_WET_CEILING/1e6:,.0f}M "
                f"({_ceiling_source}; recalibrate via alpha-engine-config's "
                "set_usage_pacing_config.py, cross-referencing /usage).")
ac3.metric("Resets in", f"{hrs_to_reset:,.0f}h",
           help=f"Next weekly reset {next_reset:%Y-%m-%d %H:%M} PT (every 7 days).")
st.progress(min(pct, 1.0),
            text=f"{ant_week_wet/1e6:,.0f}M / {WEEKLY_WET_CEILING/1e6:,.0f}M WET this week")

# =============================================================================
# Cache efficiency (Anthropic only — non-Anthropic cache fields are unreliable)
# =============================================================================
week_model = _model_since(df_model, win_start)
ant_week = df_anthropic[df_anthropic["date"] >= win_start.date().isoformat()]
week_cache_read = int(ant_week["cache_read_input_tokens"].sum())
week_cache_write = int(ant_week["cache_creation_input_tokens"].sum())
week_raw_ant = int(ant_week["total"].sum())
cache_hit_pct = (100.0 * week_cache_read / week_raw_ant) if week_raw_ant else 0.0

st.subheader("Anthropic cache efficiency", divider="gray")
cc1, cc2, cc3 = st.columns(3)
cc1.metric("Cache-read share (this week)", f"{cache_hit_pct:,.0f}%",
           help="cache_read / all Anthropic raw tokens since reset. High is good — "
                "reads are cheap on both WET and Anthropic's meter.")
cc2.metric("Cache reads", f"{week_cache_read/1e9:,.2f}B",
           help="Absolute cache-read tokens this week (reset-aligned, Anthropic only).")
cc3.metric("Cache writes", f"{week_cache_write/1e6:,.0f}M",
           help="New context written to cache — expensive; spikes on fresh sessions "
                "or repo/context switches.")
daily_cache = (ant_week.groupby("date", as_index=False)
               .agg(cache_read=("cache_read_input_tokens", "sum"),
                    cache_write=("cache_creation_input_tokens", "sum")))
fig_cache = px.bar(daily_cache, x="date",
                   y=["cache_read", "cache_write"],
                   labels={"value": "tokens", "date": "", "variable": "kind"},
                   barmode="stack")
fig_cache.update_layout(height=280, margin=dict(t=10, b=0, l=0, r=0),
                        legend_title_text="")
st.plotly_chart(fig_cache, use_container_width=True)
st.caption("Anthropic only. Target: high cache-read ratio + stable daily writes. "
           "Groom repo-sweeps (same backlog repo per chunk) and long interactive "
           "sessions improve reads.")

# =============================================================================
# Daily WET by source (all providers — informational)
# =============================================================================
st.subheader("Daily WET (by source)", divider="gray")
daily_src = (df_model.groupby(["date", "source"], as_index=False)["wet"].sum())
fig_d = px.bar(daily_src, x="date", y="wet", color="source",
               labels={"wet": "WET", "date": ""})
fig_d.update_layout(barmode="stack", height=320, legend_title_text="source",
                    margin=dict(t=10, b=0, l=0, r=0))
st.plotly_chart(fig_d, use_container_width=True)
st.caption("Includes a small non-Anthropic WET contribution (informational cross-model "
           "comparison unit) — the ceiling gauge above uses Anthropic-only WET.")

# =============================================================================
# Anthropic — WET by model
# =============================================================================
st.subheader("Anthropic — WET by model", divider="gray")
if not df_anthropic.empty:
    am1, am2 = st.columns([2, 1])
    ant_daily_mod = (df_anthropic.groupby(["date", "model"], as_index=False)["wet"].sum())
    fig_ant_m = px.bar(ant_daily_mod, x="date", y="wet", color="model",
                       labels={"wet": "WET", "date": ""})
    fig_ant_m.update_layout(barmode="stack", height=300, margin=dict(t=10, b=0, l=0, r=0))
    am1.plotly_chart(fig_ant_m, use_container_width=True)
    ant_model_tot = (df_anthropic.groupby("model", as_index=False)["wet"].sum()
                     .sort_values("wet", ascending=False))
    fig_ant_pie = px.pie(ant_model_tot, names="model", values="wet", hole=0.5)
    fig_ant_pie.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0),
                              showlegend=True)
    am2.plotly_chart(fig_ant_pie, use_container_width=True)
    am2.caption("Watch which Anthropic model drives you toward the weekly Max ceiling.")
else:
    st.caption("No Anthropic usage data yet.")

# =============================================================================
# Hourly profile heatmap (all models, WET — informational)
# =============================================================================
st.subheader("Hourly profile (WET, PT)", divider="gray")
if not df_hour.empty:
    hh = (df_hour.groupby(["date", "hour"], as_index=False)["wet"].sum())
    pivot = hh.pivot(index="hour", columns="date", values="wet").reindex(range(24))
    fig_h = px.imshow(pivot, aspect="auto", color_continuous_scale="Blues",
                      labels=dict(x="", y="hour (PT)", color="WET"))
    fig_h.update_layout(height=380, margin=dict(t=10, b=0, l=0, r=0))
    st.plotly_chart(fig_h, use_container_width=True)
    st.caption("Daytime band = interactive (laptop) work; the overnight/8-hourly "
               "band is the backlog groom's `source='groom'` usage. Irregular "
               "spikes are `source='watch'` — Fleet-SF/CI Watch resilience-agent "
               "runs, which fire only on pipeline/CI failures (config#1899). "
               "Hour-granularity data has no provider breakdown, so this includes "
               "a small non-Anthropic contribution.")

# =============================================================================
# Secondary: Anthropic raw tokens + notional $ (expander)
# =============================================================================
with st.expander("Anthropic — raw tokens + notional $"):
    daily_full = (ant_week.groupby("date", as_index=False)
                  .agg(wet=("wet", "sum"), cost_usd=("cost_usd", "sum"),
                       raw_total=("total", "sum"),
                       input=("input_tokens", "sum"), output=("output_tokens", "sum"),
                       cache_write=("cache_creation_input_tokens", "sum"),
                       cache_read=("cache_read_input_tokens", "sum"))
                  .sort_values("date", ascending=False))
    daily_full["$ (notional)"] = daily_full["cost_usd"].map(lambda v: f"${v:,.0f}")
    st.caption("`$` is API-equivalent at current public pricing — a snapshot, NOT "
               "what the Max plan actually charges (it's a flat subscription). "
               "Raw tokens are ~99% cache-reads (cheap), which is why WET is the "
               "headline here. Window: this reset cycle only — see the **API** "
               "tab for the fuller 35-day non-Anthropic + research-fleet history.")
    st.dataframe(
        daily_full[["date", "wet", "$ (notional)", "raw_total",
                    "input", "output", "cache_write", "cache_read"]],
        use_container_width=True, hide_index=True,
    )
