"""Crucible Results — §E Feedback loop, the governed auto-apply (config#1957).

The backtester may auto-apply optimized parameters to live config — but
every apply is gated (significance floor, max-change, OOS validation) and
every outcome is recorded. Gates that BLOCK on insignificance are a selling
point, so this tab shows them blocking: per-loop outcomes from the apply
audit, and the live config artifacts including the honest "never written"
state for loops whose gates have never passed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from loaders.s3_loader import load_apply_audit, load_autoapply_config_meta  # noqa: E402
from results import view_model as vm  # noqa: E402

st.title("Feedback loop — governed auto-apply")
st.caption(
    "The optimizer may promote parameters to live config only through named gates "
    "(significance floor, max-change clamp, holdout validation). Every weekly outcome "
    "is recorded; a blocked apply is the system working, not failing."
)

st.subheader("Latest apply outcomes", help="Per-optimizer outcome of the most recent weekly run: promoted, blocked (with machine-readable reasons), insufficient_data, error or disabled. consecutive_blocked_weeks is the carry-forward stall counter the evaluator grades RED at ≥4.")
audit = load_apply_audit()
rows = vm.apply_audit_rows(audit)
if rows:
    as_of = (audit or {}).get("as_of", "—")
    st.caption(f"as of {as_of} · `config/apply_audit/latest.json` (schema v{(audit or {}).get('schema_version', '?')})")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.info(
        "No apply-audit artifact yet — the producer shipped 2026-07-06 "
        "(crucible-backtester#470) and first emits on the 2026-07-11 Saturday run. "
        "This panel self-activates then."
    )

st.subheader("Live auto-apply configs", help="The four config artifacts the optimizer loops may write. NEVER WRITTEN is a true statement about that loop's gates — not a rendering gap.")
meta = load_autoapply_config_meta()
snapshot = vm.config_snapshot_rows(meta)
if snapshot:
    st.dataframe(pd.DataFrame(snapshot), use_container_width=True, hide_index=True)
    st.caption(
        "As diagnosed 2026-07-06 (config#1841): only `executor_params` has ever promoted to live; "
        "`scoring_weights` was blocked for weeks by a key-drift bug (fixed) and the significance "
        "floor now correctly blocks statistically insignificant weight changes."
    )
else:
    st.info("Config metadata unavailable.")
