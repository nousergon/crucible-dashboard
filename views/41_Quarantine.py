"""
Changelog Quarantine — Alpha Engine (private console)

Triage surface over **quarantined changelog entries**
(`s3://alpha-engine-research/changelog/quarantine/`). Vocab-validation
violators are routed here instead of `changelog/entries/` by the auto-emit
Lambdas (`_shared/vocab.py`) and the `append-changelog` composite action
(config#863), each carrying a `validation_errors` array naming the field(s)
that fell outside the allowed vocab set.

The /Changelog mining page only sees conforming `entries/`, so rejects are
invisible there. This page surfaces them keyed by date so an operator can see
what was rejected and why, then decide whether the reject was a genuine vocab
typo (fix upstream) or a schema lag worth migrating into `entries/`
(config#868).

Read-only: the "Approve & migrate to entries/" write-back is a later
increment — it needs a dashboard IAM grant for `s3:PutObject`/`DeleteObject`
on the two prefixes, which the read-only console role lacks.

**Loader:** `loaders/quarantine_loader.py`
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.quarantine_loader import load_quarantine_entries  # noqa: E402

st.title("Changelog Quarantine — vocab-rejected entries")
st.caption(
    "Entries rejected by changelog vocab validation (`changelog/quarantine/`). "
    "Each carries a `validation_errors` reason. Empty is the healthy state."
)

# --- Controls ---------------------------------------------------------------
days = st.slider("Lookback (days)", min_value=7, max_value=90, value=30, step=1)
df = load_quarantine_entries(days)

if df.empty:
    st.success(
        f"No quarantined entries in the last {days} days — vocab validation is "
        "passing for every changelog feeder (or the corpus isn't reachable "
        "from this instance)."
    )
    st.stop()

# --- KPI strip --------------------------------------------------------------
k1, k2, k3 = st.columns(3)
k1.metric(f"Quarantined ({days}d)", len(df))
last7 = df[df["ts"] >= (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7))]
k2.metric("Last 7d", len(last7))
k3.metric("Distinct subsystems", int(df["subsystem"].nunique()))

# --- Reject table -----------------------------------------------------------
st.subheader("Quarantined entries")
cols = [
    "ts_utc",
    "subsystem",
    "event_type",
    "severity",
    "actor",
    "source",
    "validation_errors",
    "summary",
    "event_id",
]
st.dataframe(df[cols], use_container_width=True, hide_index=True)

st.caption(
    "To clear an entry: fix the upstream vocab typo so the feeder re-emits a "
    "conforming entry, or — if the value is legitimate — add it to "
    "`alpha-engine-config/changelog/vocab.yaml` (additive-only) and the "
    "vendored copies in the two producers. The S3 object can then be deleted "
    "out of band."
)
