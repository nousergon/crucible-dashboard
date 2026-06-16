"""
Scanner — Alpha Engine (private console)

The TOP of the research funnel: the weekly quant filter that screens the
S&P 500+400 (~900 names) down to the ~60 candidates the sector teams see.
Shows the gates applied and the pass/fail results **broken down by sector**,
the gate-failure breakdown, and per-ticker detail — straight from
``scanner_evaluations`` in research.db. No LLM call.

The gates (recorded per ticker as pass/fail flags):
  • liquidity     — avg 20d volume vs the scanner's minimum
  • volatility    — ATR%% within band
  • balance sheet — solvency screen
  • rank cutoff   — tech_score rank within the screen
(Threshold values live in the scanner config; this page shows the recorded
outcomes, not the thresholds.)

Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome — no set_page_config (the st.navigation entrypoint in app.py owns it).
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.db_loader import get_decision_eval_dates, get_scanner_evaluations

st.markdown("### 🔭 Scanner")
st.caption(
    "The weekly quant filter: ~900 screened → ~60 candidates. Gates "
    "(liquidity · volatility · balance sheet · tech-score rank) and results "
    "per sector. Read from the recorded scan (no LLM call, no cost)."
)

eval_dates = get_decision_eval_dates(limit=30)
if not eval_dates:
    st.warning(
        "No recorded scans found in research.db (scanner_evaluations). The "
        "scanner runs on each Saturday research cycle."
    )
    st.stop()

eval_date = st.selectbox("Cycle (eval_date)", eval_dates, index=0)
df = get_scanner_evaluations(eval_date)
if df.empty:
    st.info(f"No scanner_evaluations rows for {eval_date}.")
    st.stop()

df["passed"] = pd.to_numeric(df["quant_filter_pass"], errors="coerce").fillna(0).astype(int)
df["sector"] = df["sector"].fillna("Unknown")
screened, passed = len(df), int(df["passed"].sum())

# ---------------------------------------------------------------------------
# Funnel headline
# ---------------------------------------------------------------------------
m1, m2, m3 = st.columns(3)
m1.metric("Screened", screened)
m2.metric("Passed", passed)
m3.metric("Pass rate", f"{passed / screened:.1%}" if screened else "—")

st.divider()

# ---------------------------------------------------------------------------
# Per-sector results — the core view
# ---------------------------------------------------------------------------
st.markdown("#### Results by sector")
by_sector = (
    df.groupby("sector")
    .agg(screened=("ticker", "count"), passed=("passed", "sum"))
    .reset_index()
)
by_sector["pass_rate"] = (by_sector["passed"] / by_sector["screened"]).round(3)
by_sector = by_sector.sort_values("screened", ascending=False).reset_index(drop=True)

st.dataframe(by_sector, use_container_width=True, hide_index=True)
if not by_sector.empty:
    st.caption("Pass rate by sector (a sector at 0% means the scanner gated every "
               "name in it this cycle — worth investigating).")
    st.bar_chart(by_sector.set_index("sector")["pass_rate"])

st.divider()

# ---------------------------------------------------------------------------
# Gate-failure breakdown
# ---------------------------------------------------------------------------
st.markdown("#### Why names were dropped")
failed = df[df["passed"] == 0]
g1, g2, g3 = st.columns(3)


def _fails(col: str) -> int:
    if col not in df.columns:
        return 0
    return int((pd.to_numeric(df[col], errors="coerce").fillna(1) == 0).sum())


g1.metric("Failed liquidity gate", _fails("liquidity_pass"))
g2.metric("Failed volatility gate", _fails("volatility_pass"))
g3.metric("Failed balance-sheet gate", _fails("balance_sheet_pass"))

if "filter_fail_reason" in failed.columns and not failed.empty:
    reasons = (
        failed["filter_fail_reason"].fillna("(unspecified)").value_counts()
        .rename_axis("filter_fail_reason").reset_index(name="n")
    )
    st.caption("Primary fail reason distribution (one reason per dropped name):")
    st.dataframe(reasons, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Per-ticker detail
# ---------------------------------------------------------------------------
st.markdown("#### Per-ticker detail")
fc1, fc2 = st.columns([2, 1])
with fc1:
    sectors = sorted(df["sector"].unique().tolist())
    pick = st.multiselect("Sectors", sectors, default=sectors)
with fc2:
    outcome = st.radio("Outcome", ["All", "Passed", "Failed"], horizontal=True)

view = df[df["sector"].isin(pick)]
if outcome == "Passed":
    view = view[view["passed"] == 1]
elif outcome == "Failed":
    view = view[view["passed"] == 0]

cols = ["ticker", "sector", "passed", "scan_path", "tech_score", "filter_fail_reason",
        "rsi_14", "atr_pct", "price_vs_ma200", "avg_volume_20d", "current_price"]
st.caption(f"{len(view)} of {screened} names.")
st.dataframe(
    view[[c for c in cols if c in view.columns]]
    .sort_values(["passed", "tech_score"], ascending=[False, False]),
    use_container_width=True, hide_index=True,
)

st.caption(
    "Downstream of the scanner: **Sector Team Review** (what each team did with "
    "its slice), **CIO Review** (the committee gate), **Decision Review** (one "
    "ticker through the whole funnel)."
)
