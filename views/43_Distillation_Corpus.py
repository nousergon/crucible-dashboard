"""Distillation Corpus — Alpha Engine (private console)

Tracks the **SFT distillation corpus**: the (input → teacher-output) pairs
captured off every research/advisor LLM call (`_sft_raw` sinks) that will train
a distilled open-source specialist (EPIC config#1542, first target = the
quant-calibrator, config#1135).

Reads the deduped stats artifact
``decision_artifacts/distillation/corpus_stats/latest.json`` written by
crucible-research ``scripts/corpus_stats.py`` each Saturday (config#1544) — no
LLM call, no cost. The headline number is the **kill-gate trigger**: deduped,
single-teacher quant-calibrator pairs vs the ~1000 target. When it crosses, the
config#1542 90-day distill-or-shelve clock starts.

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit chrome
— no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import load_distillation_corpus_stats


def _kv_df(d: dict, key_name: str, val_name: str) -> pd.DataFrame:
    """Render a {label: count} dict as a sorted two-column frame."""
    rows = sorted(d.items(), key=lambda kv: kv[1], reverse=True)
    return pd.DataFrame(rows, columns=[key_name, val_name])


st.markdown("### 🔬 Distillation Corpus")
st.caption(
    "Growth of the SFT distillation corpus — the teacher-output pairs that will "
    "train a distilled quant-calibrator (config#1135). The headline gauge is the "
    "kill-gate trigger: deduped, single-teacher **quant-calibrator** pairs vs the "
    "~1000 target (config#1542). Read from the recorded corpus (no LLM call)."
)

stats = load_distillation_corpus_stats()

if not stats:
    st.warning(
        "No corpus-stats artifact published yet. It is written by crucible-research "
        "`scripts/corpus_stats.py` as a post-step of each Saturday research run "
        "(config#1544). The panel renders once the first artifact lands."
    )
    st.stop()

trg = stats.get("trigger", {})
totals = stats.get("totals", {})
cap = stats.get("capture", {})

target = trg.get("target_pairs", 1000)
quant_n = trg.get("deduped_single_teacher", 0)
pct = trg.get("pct", 0.0)

# ── KPI strip ──────────────────────────────────────────────────────────────
c = st.columns([1.3, 1.2, 1, 1, 1.2])
c[0].metric("Quant-calibrator pairs", f"{quant_n:,} / {target:,}", help=(
    "Deduped, dominant-teacher-segregated sector_quant pairs — the config#1542 "
    "kill-gate trigger metric."))
c[1].metric("Progress to gate", f"{pct:.1f}%")
c[2].metric("Deduped (all tasks)", f"{totals.get('deduped_pairs', 0):,}")
c[3].metric("Dupes dropped", f"{totals.get('duplicates_dropped', 0):,}")
c[4].metric("Last captured", cap.get("last_captured_date") or "—")

# ── Trigger status ─────────────────────────────────────────────────────────
st.progress(min(1.0, quant_n / target if target else 0.0))
if trg.get("crossed"):
    st.success(
        f"**Trigger crossed** — {quant_n:,} ≥ {target:,} quant-calibrator pairs. "
        "The config#1542 90-day distill-or-shelve clock "
        + ("has started." if trg.get("clock_started") else "should start (see #1542).")
    )
else:
    remaining = max(0, target - quant_n)
    st.info(
        f"**Clock not started** — {remaining:,} more deduped single-teacher "
        f"quant-calibrator pairs to the ~{target:,} gate "
        f"(dominant teacher: `{trg.get('dominant_teacher', '—')}`)."
    )

# ── Capture freshness ──────────────────────────────────────────────────────
missing = cap.get("missing_saturdays") or []
if missing:
    st.warning(
        "⚠️ **Missing Saturday captures** (each is a permanently-lost training "
        f"run — config#1134): {', '.join(missing)}. Every un-captured Saturday "
        "pushes the gate further out."
    )

# ── Growth over time ───────────────────────────────────────────────────────
growth = stats.get("growth") or []
if len(growth) >= 1:
    df = pd.DataFrame(growth)
    df["date"] = pd.to_datetime(df["date"])
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["date"], y=df["added"], name="Added / run",
                         marker_color="#9ecae1", opacity=0.8, yaxis="y1"))
    fig.add_trace(go.Scatter(x=df["date"], y=df["cumulative"], name="Cumulative (deduped)",
                             mode="lines+markers", line=dict(color="#1f77b4", width=2.5),
                             yaxis="y2"))
    fig.add_hline(y=target, line_dash="dot", line_color="#d62728",
                  annotation_text=f"gate {target:,}", yref="y2")
    fig.update_layout(
        title="Corpus growth (deduped, all tasks)",
        xaxis=dict(title="Capture date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Added / run", showgrid=False),
        yaxis2=dict(title="Cumulative pairs", overlaying="y", side="right"),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=60),
    )
    st.plotly_chart(fig, use_container_width=True)

# ── Breakdowns ─────────────────────────────────────────────────────────────
st.divider()
b1, b2 = st.columns(2)
with b1:
    st.markdown("**By task** (deduped)")
    st.dataframe(_kv_df(stats.get("by_task", {}), "task", "pairs"),
                 hide_index=True, use_container_width=True)
    st.markdown("**By producer**")
    st.dataframe(_kv_df(stats.get("by_producer", {}), "producer", "pairs"),
                 hide_index=True, use_container_width=True)
with b2:
    st.markdown("**By teacher** (segregate — never blend versions)")
    st.dataframe(_kv_df(stats.get("by_teacher", {}), "teacher_model", "pairs"),
                 hide_index=True, use_container_width=True)
    st.markdown("**By source · schema**")
    st.dataframe(_kv_df(stats.get("by_source", {}), "source", "pairs"),
                 hide_index=True, use_container_width=True)
    st.caption(
        "schema_version: "
        + " · ".join(f"v{k}={v}" for k, v in sorted(stats.get("by_schema_version", {}).items()))
        + (f"  ·  unparseable={totals.get('unparseable', 0)}")
    )

st.caption(
    f"Artifact generated {stats.get('generated_at', '—')} · "
    "producer: crucible-research `scripts/corpus_stats.py` · "
    "gate + phasing: config#1542 · observability: config#1544."
)
