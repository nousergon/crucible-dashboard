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
fast-follow ``source='groom'`` from the GHA groom).

Anthropic publishes **no exact Max 20x weekly limit** (it's demand-variable; the
in-app ``/usage`` % is the only ground truth). So the "% of ceiling" gauge below
uses an empirically-calibrated constant — adjust ``WEEKLY_WET_CEILING`` once you
see a real throttle, cross-referencing ``/usage``.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import streamlit as st

from loaders.s3_loader import load_claude_code_usage

# Calibrate-me ceiling: Brian ran ~700M WET/week with NO throttle, so the true
# weekly limit is >= that. 1.0B is a headroom placeholder — lower it toward the
# first observed throttle (cross-check Settings -> Usage in the Claude app).
WEEKLY_WET_CEILING = 1_000_000_000

st.divider()
st.title("Claude Code Usage")
st.caption(
    "Brian's Claude **Max 20x** consumption, in **WET** (weighted effective tokens — "
    "price-independent). Raw tokens + notional $ shown underneath. There is no published "
    "Max weekly limit, so the ceiling below is a calibrate-me estimate — adjust against `/usage`."
)

df_model, df_hour = load_claude_code_usage(n_days=35)

if df_model.empty:
    st.info(
        "No usage data yet. Install the hourly producer on the laptop:\n\n"
        "`bash scripts/install_usage_launchd.sh`  (in alpha-engine-config)\n\n"
        "It writes `claude_code_usage/{source}/{date}.json` to S3; this page reads it."
    )
    st.stop()

today = date.today()
week_start = today - timedelta(days=today.weekday())          # Monday
roll_start = today - timedelta(days=6)                          # rolling 7d incl today

wtd = df_model[df_model["date"] >= week_start.isoformat()]["wet"].sum()
roll = df_model[df_model["date"] >= roll_start.isoformat()]["wet"].sum()
pct = (roll / WEEKLY_WET_CEILING) if WEEKLY_WET_CEILING else 0.0

# ---- headline gauges -------------------------------------------------------
c1, c2, c3 = st.columns(3)
c1.metric("Rolling 7-day WET", f"{roll/1e6:,.0f}M",
          help="Sum of WET over the trailing 7 days (incl. today).")
c2.metric("Week-to-date WET", f"{wtd/1e6:,.0f}M",
          help=f"Since Monday {week_start.isoformat()}.")
c3.metric("% of weekly ceiling", f"{pct*100:,.0f}%",
          help=f"Rolling-7d WET / {WEEKLY_WET_CEILING/1e6:,.0f}M (calibrate-me).")
st.progress(min(pct, 1.0), text=f"{roll/1e6:,.0f}M / {WEEKLY_WET_CEILING/1e6:,.0f}M WET (7d)")

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
               "band is the backlog groom's `source='groom'` usage.")

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
