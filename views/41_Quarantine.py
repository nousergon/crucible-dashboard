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

"Approve & migrate to entries/" writes the entry to the equivalent
`changelog/entries/{date}/{event_id}.json` key and deletes the quarantine
copy — requires the dashboard role's `s3:PutObject`/`DeleteObject` grant on
both prefixes (config#868; codified in alpha-engine-config
`iam/alpha-engine-dashboard-role/
alpha-engine-dashboard-changelog-quarantine-writeback.json`). Until an
operator applies that grant live, the button surfaces a clear error rather
than crashing the page.

**Loader:** `loaders/quarantine_loader.py`
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.quarantine_loader import (  # noqa: E402
    load_quarantine_entries,
    migrate_quarantine_entry,
)

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
    "vendored copies in the two producers. Then Approve & migrate below (or "
    "delete the quarantine object out of band)."
)

# --- Approve & migrate --------------------------------------------------
st.subheader("Approve & migrate to entries/")
st.caption(
    "Moves a quarantined entry (unmodified) to `changelog/entries/` and "
    "removes the quarantine copy — use once the vocab value has been added "
    "to `vocab.yaml` (schema-lag case), not for genuine typos (fix "
    "upstream instead). Requires the dashboard role's S3 write grant "
    "(config#868); until an operator applies it live, this will show a "
    "clear error rather than crash the page."
)
for _, row in df.iterrows():
    day = row["day"].isoformat() if pd.notna(row["day"]) else None
    event_id = row["event_id"]
    label = f"{row['ts_utc']} · {row['subsystem']} · {event_id}"
    btn_col, msg_col = st.columns([1, 3])
    with btn_col:
        clicked = st.button(
            "Approve & migrate",
            key=f"migrate_{event_id}",
            disabled=not day or not event_id,
        )
    if clicked:
        ok, msg = migrate_quarantine_entry(day, event_id)
        if ok:
            st.success(f"{label} — {msg}")
            load_quarantine_entries.clear()
            st.rerun()
        else:
            with msg_col:
                st.error(f"{label} — {msg}")
