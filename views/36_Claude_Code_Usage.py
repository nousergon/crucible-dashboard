"""
Claude Code Usage — Alpha Engine (private console)

How much of Brian's Claude **Max 20x** allocation is actually being consumed —
to pace the overnight backlog-groom allocation (~1/14 of weekly per night).

Headline unit is **WET (weighted effective tokens)** — Opus-input-equivalent
tokens from frozen, price-INDEPENDENT ratios, so it stays comparable across
re-pricings. Raw tokens are the lossless truth (cache-read-dominated, so a poor
headline); the $ figure is a notional snapshot. Source: the per-(source,date)
JSON at ``claude_code_usage/{source}/{date}.json``, produced by
alpha-engine-config ``scripts/collect_usage.py`` (hourly launchd on the laptop;
run-scoped ``source='groom'`` from the GHA groom; run-scoped ``source='watch'``
from the Fleet-SF/CI Watch agent runs — config#1899).

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
# 2026-07-08: ~706M WET @ /usage 83% -> 850M (console was ~83% @ 853M anchor).
# 2026-07-08 earlier: ~708M WET @ /usage 83% -> 853M (console was 105% @ 674M anchor).
# 2026-07-06: 175.2M WET @ /usage 26% -> 674M (was 1.14B from 2026-06-28 anchor).
# WET is our price-independent proxy, NOT Anthropic's actual meter — the ceiling
# is a scale factor so the console % tracks /usage, not a published limit.
WEEKLY_WET_CEILING = 850_000_000

# Anthropic's Max weekly limit resets every 7 days. The gauge MUST measure WET
# over the same reset-aligned window the limit uses — a trailing-7d window would
# read ~78% moments after a reset drops /usage to ~0%, giving false headroom
# (the number #1348's dynamic allocation depends on). Anchor = one observed reset
# instant from /usage (2026-07-05 9:00pm America/Los_Angeles); the current window
# is [most recent reset <= now, next reset). Buckets are PT, so we work in PT.
# If Anthropic ever shifts the reset cadence, update this anchor from /usage.
# Window math itself is the shared krepis.usage_pacing.reset_window primitive
# (config#1351 / config#1722) — also consumed by alpha-engine-config's
# scripts/groom_budget.py and the dispatcher Lambda, so all three stay bit-for-bit in sync.
_PT = ZoneInfo("America/Los_Angeles")
WEEKLY_RESET_ANCHOR = datetime(2026, 7, 12, 21, 0)   # PT, naive — Sunday 9pm PT
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
st.title("Claude Code Usage")
st.caption(
    "Brian's Claude **Max 20x** consumption, in **WET** (weighted effective tokens — "
    "price-independent). Raw tokens + notional $ shown underneath. The weekly gauge is "
    "measured over Anthropic's **reset-aligned** window (resets every 7d) and compared to "
    "a calibrated ceiling — both approximate, refined against `/usage`."
)

df_model, df_hour = load_claude_code_usage(n_days=35)

if df_model.empty:
    st.info(
        "No usage data yet. Install the hourly producer on the laptop:\n\n"
        "`bash scripts/install_usage_launchd.sh`  (in alpha-engine-config)\n\n"
        "It writes `claude_code_usage/{source}/{date}.json` to S3; this page reads it."
    )
    st.stop()

now_pt = datetime.now(_PT).replace(tzinfo=None)
win_start, next_reset = reset_window(now_pt, WEEKLY_RESET_ANCHOR, WEEKLY_PERIOD)
week_wet = _wet_since(df_hour, df_model, win_start)
roll = df_model[df_model["date"] >= (now_pt.date() - timedelta(days=6)).isoformat()]["wet"].sum()
pct = (week_wet / WEEKLY_WET_CEILING) if WEEKLY_WET_CEILING else 0.0
hrs_to_reset = max(0, (next_reset - now_pt).total_seconds()) / 3600.0

# ---- headline gauges -------------------------------------------------------
c1, c2, c3 = st.columns(3)
c1.metric("This week's WET (since reset)", f"{week_wet/1e6:,.0f}M",
          help=f"Reset-aligned window start {win_start:%Y-%m-%d %H:%M} PT. "
               f"Rolling-7d (informational): {roll/1e6:,.0f}M.")
c2.metric("% of weekly ceiling", f"{pct*100:,.0f}%",
          help=f"This week's WET / {WEEKLY_WET_CEILING/1e6:,.0f}M (calibrated "
               f"2026-07-08 @ /usage 83%, reset Sun 9pm PT; re-calibrate every few days).")
c3.metric("Resets in", f"{hrs_to_reset:,.0f}h",
          help=f"Next weekly reset {next_reset:%Y-%m-%d %H:%M} PT (every 7 days).")
st.progress(min(pct, 1.0),
            text=f"{week_wet/1e6:,.0f}M / {WEEKLY_WET_CEILING/1e6:,.0f}M WET this week")

# ---- cache efficiency (load-bearing for quota pacing) ----------------------
week_model = _model_since(df_model, win_start)
week_cache_read = int(week_model["cache_read_input_tokens"].sum())
week_cache_write = int(week_model["cache_creation_input_tokens"].sum())
week_raw = int(week_model["total"].sum())
cache_hit_pct = (100.0 * week_cache_read / week_raw) if week_raw else 0.0

st.subheader("Cache efficiency")
cc1, cc2, cc3 = st.columns(3)
cc1.metric("Cache-read share (this week)", f"{cache_hit_pct:,.0f}%",
           help="cache_read / all raw tokens since reset. High is good — "
                "reads are cheap on both WET and Anthropic's meter.")
cc2.metric("Cache reads", f"{week_cache_read/1e9:,.2f}B",
           help="Absolute cache-read tokens this week (reset-aligned).")
cc3.metric("Cache writes", f"{week_cache_write/1e6:,.0f}M",
           help="New context written to cache — expensive; spikes on fresh sessions "
                "or repo/context switches.")
daily_cache = (df_model.groupby("date", as_index=False)
               .agg(cache_read=("cache_read_input_tokens", "sum"),
                    cache_write=("cache_creation_input_tokens", "sum")))
fig_cache = px.bar(daily_cache, x="date",
                   y=["cache_read", "cache_write"],
                   labels={"value": "tokens", "date": "", "variable": "kind"},
                   barmode="stack")
fig_cache.update_layout(height=280, margin=dict(t=10, b=0, l=0, r=0),
                        legend_title_text="")
st.plotly_chart(fig_cache, use_container_width=True)
st.caption("Target: high cache-read ratio + stable daily writes. Groom repo-sweeps "
           "(same backlog repo per chunk) and long interactive sessions improve reads.")

# ---- daily totals, stacked by source --------------------------------------
st.subheader("Daily WET (by source)")
daily_src = (df_model.groupby(["date", "source"], as_index=False)["wet"].sum())
fig_d = px.bar(daily_src, x="date", y="wet", color="source",
               labels={"wet": "WET", "date": ""})
fig_d.update_layout(barmode="stack", height=320, legend_title_text="source",
                    margin=dict(t=10, b=0, l=0, r=0))
st.plotly_chart(fig_d, use_container_width=True)

# ---- by-model split (load-bearing: which model drives you toward a cap) -----
st.subheader("WET by model")
mcol1, mcol2 = st.columns([2, 1])
daily_mod = (df_model.groupby(["date", "model"], as_index=False)["wet"].sum())
fig_m = px.bar(daily_mod, x="date", y="wet", color="model",
               labels={"wet": "WET", "date": ""})
fig_m.update_layout(barmode="stack", height=300, margin=dict(t=10, b=0, l=0, r=0))
mcol1.plotly_chart(fig_m, use_container_width=True)
model_tot = (df_model.groupby("model", as_index=False)["wet"].sum()
             .sort_values("wet", ascending=False))
fig_pie = px.pie(model_tot, names="model", values="wet", hole=0.5)
fig_pie.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0),
                      showlegend=True)
mcol2.plotly_chart(fig_pie, use_container_width=True)
mcol2.caption("Watch the split: it shows whether **Opus** or **Sonnet** is "
              "driving you toward whichever weekly cap bites first.")

# ---- hourly profile heatmap (hour-of-day x date) ---------------------------
st.subheader("Hourly profile (WET, PT)")
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

# ---- secondary: raw tokens + notional $ ------------------------------------
with st.expander("Raw tokens + notional $ (secondary)"):
    daily_full = (df_model.groupby("date", as_index=False)
                  .agg(wet=("wet", "sum"), cost_usd=("cost_usd", "sum"),
                       raw_total=("total", "sum"),
                       input=("input_tokens", "sum"), output=("output_tokens", "sum"),
                       cache_write=("cache_creation_input_tokens", "sum"),
                       cache_read=("cache_read_input_tokens", "sum"))
                  .sort_values("date", ascending=False))
    daily_full["$ (notional)"] = daily_full["cost_usd"].map(lambda v: f"${v:,.0f}")
    st.caption("`$` is API-equivalent at current pricing — a snapshot, NOT what the "
               "Max plan charges. Raw tokens are ~99% cache-reads (cheap), which is why "
               "WET is the headline, not raw totals.")
    st.dataframe(
        daily_full[["date", "wet", "$ (notional)", "raw_total",
                    "input", "output", "cache_write", "cache_read"]],
        use_container_width=True, hide_index=True,
    )
