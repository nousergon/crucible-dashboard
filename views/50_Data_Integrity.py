"""
50_Data_Integrity.py — market-value integrity status tile (System & Ops).

config#2458 (L4 of the market-value-integrity framework, alpha-engine-config
#1277): a green/amber/red rollup tile over the framework's phases, plus
per-number provenance drill-down for the flagged rows.

Framework status as of this page's writing:
  L1 cross-source agreement observer — SHIPPED (nousergon-data#728,
     2026-07-10). This tile's rollup is wired to L1 ONLY.
  L2 data-quality validation gates    — NOT SHIPPED. No queryable gate
     surface exists yet; not wired into this tile.
  L3 T+1 NAV reconciliation divergence — NOT SHIPPED. Same as L2.

This page therefore reflects L1 cross-source agreement coverage only. A
green tile here means "no cross-source disagreement observed in the most
recent settled-closes partition" — it does NOT mean the market-value
figures have cleared L2 validation gates or L3 NAV reconciliation, because
those signals do not exist yet. Once L2/L3 ship a queryable gate-status
surface, ``loaders/data_integrity_loader.py`` gains a signal-gathering
function per phase and this tile's rollup absorbs it with no rewrite (see
that loader's module docstring).
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_integrity_status import dot_icon, rollup_dot
from loaders.data_integrity_loader import gather_data_integrity_signals

st.title("🧬 Data Integrity")
st.caption(
    "Market-value integrity rollup — **L1 (cross-source agreement) only.** "
    "L2 (data-quality validation gates) and L3 (T+1 NAV reconciliation "
    "divergence) have not shipped yet (alpha-engine-config#1277); this tile "
    "does not reflect them. 🟢 no disagreement observed · 🟡 single-source / "
    "flagged cell(s) · 🔴 a cross-source quarantine (settled closes "
    "disagree beyond tolerance)."
)

signals = gather_data_integrity_signals()
overall = rollup_dot(signals)

top = st.columns(len(signals) + 1 or 1)
top[0].metric("Overall", f"{dot_icon(overall)} {overall.upper()}")
for i, sig in enumerate(signals, start=1):
    top[i].metric(
        f"{dot_icon(sig.dot)} {sig.phase}",
        f"{sig.flagged_count}/{sig.total_count} flagged",
    )

st.divider()

for sig in signals:
    st.subheader(f"{dot_icon(sig.dot)} {sig.phase} — {sig.label}")
    if sig.note:
        st.info(sig.note, icon="ℹ️")
    if sig.total_count == 0:
        st.caption("No daily_closes partition available in the lookback window.")
        continue
    if not sig.detail:
        st.caption(f"All {sig.total_count} row(s) clean — no flagged or quarantined cells.")
        continue
    st.dataframe(
        pd.DataFrame(list(sig.detail))[
            [c for c in ("ticker", "xsource_status", "xsource_flagged",
                          "xsource_agreement_bps", "xsource_provenance")
             if sig.detail and c in sig.detail[0]]
        ],
        use_container_width=True,
        hide_index=True,
    )

st.divider()
with st.expander("What's not covered yet"):
    st.markdown(
        "- **L2 (data-quality validation gates):** no decomposed sub-issue "
        "exists yet; alpha-engine-config#1277 still describes this as a "
        "future phase.\n"
        "- **L3 (T+1 NAV reconciliation):** same — not shipped, not wired.\n\n"
        "Both are blocking dependencies for a full L1+L2+L3 tile, not "
        "something this page re-derives dashboard-side."
    )
