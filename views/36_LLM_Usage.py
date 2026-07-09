"""
LLM Usage — Alpha Engine (private console)

How much of Brian's LLM token budget is being consumed, split by provider.
**Anthropic** models (Opus/Sonnet/Haiku/Fable) track against the Claude **Max 20x**
weekly ceiling in **WET** (weighted effective tokens — Opus-input-equivalent,
price-independent). **Non-Anthropic** models (DeepSeek, etc.) are tracked
separately in raw tokens + actual API cost since they don't count toward Max.

Source: the per-(source,date) JSON at ``claude_code_usage/{source}/{date}.json``,
produced by alpha-engine-config ``scripts/collect_usage.py`` (hourly launchd on
the laptop; run-scoped ``source='groom'`` from the GHA groom; run-scoped
``source='watch'`` from the Fleet-SF/CI Watch agent runs — config#1899).

Anthropic publishes **no exact Max 20x weekly limit** (it's demand-variable; the
in-app ``/usage`` % is the only ground truth). So the "% of ceiling" gauge below
uses an empirically-calibrated constant — adjust ``WEEKLY_WET_CEILING`` once you
see a real throttle, cross-referencing ``/usage``.
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

from loaders.s3_loader import load_claude_code_usage

# Empirically calibrated against /usage (all-models) every few days:
#   ceiling = reset_aligned_week_wet / usage_fraction
# 2026-07-08: ~708M WET @ /usage 83% -> 850M (console was 105% @ 674M anchor).
# 2026-07-06: 175.2M WET @ /usage 26% -> 674M (was 1.14B from 2026-06-28 anchor).
# WET is our price-independent proxy, NOT Anthropic's actual meter — the ceiling
# is a scale factor so the console % tracks /usage, not a published limit.
WEEKLY_WET_CEILING = 850_000_000

# Anthropic's Max weekly limit resets every 7 days. The gauge MUST measure WET
# over the same reset-aligned window the limit uses — a trailing-7d window would
# read ~78% moments after a reset drops /usage to ~0%, giving false headroom
# (the number #1348's dynamic allocation depends on). Anchor = one observed reset
# instant from /usage (2026-06-28 8:59pm America/Los_Angeles); the current window
# is [most recent reset <= now, next reset). Buckets are PT, so we work in PT.
# If Anthropic ever shifts the reset cadence, update this anchor from /usage.
# Window math itself is the shared krepis.usage_pacing.reset_window primitive
# (config#1351 / config#1722) — also consumed by alpha-engine-config's
# scripts/groom_budget.py, so the two stay bit-for-bit in sync.
_PT = ZoneInfo("America/Los_Angeles")
WEEKLY_RESET_ANCHOR = datetime(2026, 6, 28, 20, 59)   # PT, naive
WEEKLY_PERIOD = timedelta(days=7)


def _wet_since(df_hour: pd.DataFrame, df_model: pd.DataFrame, start: datetime) -> float:
    """Sum WET at/after the PT datetime ``start``. Prefer hour-precision (df_hour);
    fall back to day-granularity (df_model) if the hourly frame is empty."""
    if not df_hour.empty:
        dts = pd.to_datetime(df_hour["date"]) + pd.to_timedelta(df_hour["hour"], unit="h")
        return float(df_hour.loc[dts >= pd.Timestamp(start), "wet"].sum())
    return float(df_model.loc[df_model["date"] >= start.date().isoformat(), "wet"].sum())


def _model_since(df_model: pd.DataFrame, start: datetime) -> pd.DataFrame:
    """Rows at/after ``start`` (day-granularity; cache fields live on df_model)."""
    return df_model.loc[df_model["date"] >= start.date().isoformat()]


st.divider()
st.title("LLM Usage")
st.caption(
    "Brian's LLM consumption, split by provider. **Anthropic** (Opus / Sonnet / "
    "Haiku / Fable) is tracked in **WET** (weighted effective tokens — "
    "price-independent) against the Claude **Max 20x** weekly ceiling. "
    "**Non-Anthropic** (DeepSeek, etc.) is tracked separately in raw tokens + "
    "API-equivalent cost — it does **not** count toward Max."
)

df_model, df_hour = load_claude_code_usage(n_days=35)

if df_model.empty:
    st.info(
        "No usage data yet. Install the hourly producer on the laptop:\n\n"
        "`bash scripts/install_usage_launchd.sh`  (in alpha-engine-config)\n\n"
        "It writes `claude_code_usage/{source}/{date}.json` to S3; this page reads it."
    )
    st.stop()

# --- provider splits -----------------------------------------------------------
df_anthropic = df_model[df_model["provider"] == "anthropic"]
df_non_anthropic = df_model[df_model["provider"] == "non-anthropic"]

now_pt = datetime.now(_PT).replace(tzinfo=None)
win_start, next_reset = reset_window(now_pt, WEEKLY_RESET_ANCHOR, WEEKLY_PERIOD)

# Anthropic metrics (WET toward Max ceiling)
week_wet = _wet_since(df_hour, df_model, win_start)  # all WET (for % of ceiling)
ant_week_wet = float(df_anthropic.loc[df_anthropic["date"] >= win_start.date().isoformat(), "wet"].sum())
roll = df_model[df_model["date"] >= (now_pt.date() - timedelta(days=6)).isoformat()]["wet"].sum()
pct = (week_wet / WEEKLY_WET_CEILING) if WEEKLY_WET_CEILING else 0.0
hrs_to_reset = max(0, (next_reset - now_pt).total_seconds()) / 3600.0

# Non-Anthropic metrics (raw tokens + API cost)
na_week = df_non_anthropic[df_non_anthropic["date"] >= win_start.date().isoformat()]
na_week_tokens = int(na_week["total"].sum()) if not na_week.empty else 0
na_week_cost = float(na_week["cost_usd"].sum()) if not na_week.empty else 0.0

# =============================================================================
# HEADLINE: Anthropic (Max ceiling)
# =============================================================================
st.subheader("Anthropic — Max 20x ceiling", divider="gray")
ac1, ac2, ac3 = st.columns(3)
ac1.metric("This week's WET (Anthropic only)", f"{ant_week_wet/1e6:,.0f}M",
           help=f"Reset-aligned window start {win_start:%Y-%m-%d %H:%M} PT. "
                f"All-model WET (incl. non-Anthropic): {week_wet/1e6:,.0f}M. "
                f"Rolling-7d (informational): {roll/1e6:,.0f}M.")
ac2.metric("% of weekly ceiling", f"{pct*100:,.0f}%",
           help=f"This week's WET (all models) / {WEEKLY_WET_CEILING/1e6:,.0f}M "
                f"(calibrated 2026-07-08 @ /usage 83% → 850M; re-calibrate every few days).")
ac3.metric("Resets in", f"{hrs_to_reset:,.0f}h",
           help=f"Next weekly reset {next_reset:%Y-%m-%d %H:%M} PT (every 7 days).")
st.progress(min(pct, 1.0),
            text=f"{week_wet/1e6:,.0f}M / {WEEKLY_WET_CEILING/1e6:,.0f}M WET this week")

# =============================================================================
# HEADLINE: Non-Anthropic (API cost)
# =============================================================================
st.subheader("Non-Anthropic — API cost", divider="orange")
nc1, nc2, nc3 = st.columns(3)
nc1.metric("Raw tokens this week", f"{na_week_tokens/1e6:,.0f}M",
           help="All non-Anthropic providers since the last reset. "
                "These do NOT count toward the Max ceiling.")
nc2.metric("API cost this week", f"${na_week_cost:,.2f}",
           help="Notional API-equivalent cost at current public pricing. "
                "DeepSeek V4-Pro: $1.74/M in, $3.48/M out (non-promo, post-May-31 2026).")
nc3.metric("Source", f"{na_week['source'].nunique() if not na_week.empty else 0}",
           help="Number of distinct sources contributing non-Anthropic usage.")

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
# Daily WET by source (all providers)
# =============================================================================
st.subheader("Daily WET (by source)", divider="gray")
daily_src = (df_model.groupby(["date", "source"], as_index=False)["wet"].sum())
fig_d = px.bar(daily_src, x="date", y="wet", color="source",
               labels={"wet": "WET", "date": ""})
fig_d.update_layout(barmode="stack", height=320, legend_title_text="source",
                    margin=dict(t=10, b=0, l=0, r=0))
st.plotly_chart(fig_d, use_container_width=True)

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
# Non-Anthropic — raw tokens + API cost by model
# =============================================================================
st.subheader("Non-Anthropic — tokens + cost by model", divider="orange")
if not df_non_anthropic.empty:
    na_m1, na_m2 = st.columns([2, 1])
    # Daily raw tokens by model
    na_daily_mod = (df_non_anthropic.groupby(["date", "model"], as_index=False)["total"].sum())
    fig_na_tok = px.bar(na_daily_mod, x="date", y="total", color="model",
                        labels={"total": "raw tokens", "date": ""})
    fig_na_tok.update_layout(barmode="stack", height=300, margin=dict(t=10, b=0, l=0, r=0))
    na_m1.plotly_chart(fig_na_tok, use_container_width=True)
    # Cost donut
    na_model_cost = (df_non_anthropic.groupby("model", as_index=False)["cost_usd"].sum()
                     .sort_values("cost_usd", ascending=False))
    total_na_cost = na_model_cost["cost_usd"].sum()
    fig_na_pie = px.pie(na_model_cost, names="model", values="cost_usd", hole=0.5)
    fig_na_pie.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0),
                             showlegend=True)
    na_m2.plotly_chart(fig_na_pie, use_container_width=True)
    na_m2.caption(
        f"API-equivalent cost at current public pricing. "
        f"Total tracked: **${total_na_cost:,.2f}**. "
        f"DeepSeek V4-Pro: $1.74/M in, $3.48/M out (non-promo, post-May-31 2026)."
    )

    # Daily cost by model (stacked bar)
    st.subheader("Non-Anthropic — daily API cost", divider="orange")
    na_daily_cost = (df_non_anthropic.groupby(["date", "model"], as_index=False)["cost_usd"].sum())
    fig_na_cost = px.bar(na_daily_cost, x="date", y="cost_usd", color="model",
                         labels={"cost_usd": "API cost ($)", "date": ""})
    fig_na_cost.update_layout(barmode="stack", height=280, margin=dict(t=10, b=0, l=0, r=0))
    st.plotly_chart(fig_na_cost, use_container_width=True)
else:
    st.caption("No non-Anthropic usage data yet. (Expected — the provider-aware "
               "collector was just deployed; data will appear as new transcripts "
               "with non-Anthropic models are collected.)")

# =============================================================================
# Hourly profile heatmap (all models, WET)
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
               "runs, which fire only on pipeline/CI failures (config#1899).")

# =============================================================================
# Secondary: raw tokens + notional $ (all models, expander)
# =============================================================================
with st.expander("Raw tokens + notional $ (all models)"):
    daily_full = (df_model.groupby("date", as_index=False)
                  .agg(wet=("wet", "sum"), cost_usd=("cost_usd", "sum"),
                       raw_total=("total", "sum"),
                       input=("input_tokens", "sum"), output=("output_tokens", "sum"),
                       cache_write=("cache_creation_input_tokens", "sum"),
                       cache_read=("cache_read_input_tokens", "sum"))
                  .sort_values("date", ascending=False))
    daily_full["$ (notional)"] = daily_full["cost_usd"].map(lambda v: f"${v:,.0f}")
    st.caption("`$` is API-equivalent at current public pricing — a snapshot, NOT "
               "what the Max plan charges. Non-Anthropic cost is the actual "
               "API-equivalent since those models bill per-token outside of Max. "
               "Raw tokens are ~99% cache-reads (cheap), which is why WET is the "
               "headline for Anthropic.")
    st.dataframe(
        daily_full[["date", "wet", "$ (notional)", "raw_total",
                    "input", "output", "cache_write", "cache_read"]],
        use_container_width=True, hide_index=True,
    )