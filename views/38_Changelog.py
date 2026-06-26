"""
Changelog — Alpha Engine (private console)

Mining surface over the unified **changelog event-lake**
(`s3://alpha-engine-research/changelog/entries/`, schema 1.0.0) — the fleet's
single source for production-failure data (flow-doctor SOTA arc,
alpha-engine-config#1273, Phase D).

Three feeders write the same shape into the lake: flow-doctor's s3 sink (rich
captures from the big handlers), changelog-cloudwatch-mirror (every Lambda's
ERROR/CRITICAL/timeout), and changelog-incident-mirror (SNS alerts). This page
lets you slice that corpus: incident volume + trend, breakdown by subsystem /
severity / source, the most frequent error signatures, and a recent-incident
table.

**Loader:** `loaders/changelog_loader.py`
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.changelog_loader import load_changelog_entries  # noqa: E402

st.title("Changelog — failure event-lake")
st.caption(
    "Unified production-failure corpus (`changelog/entries/`, schema 1.0.0). "
    "Fed by flow-doctor + the CloudWatch & SNS mirrors."
)

# --- Controls ---------------------------------------------------------------
days = st.slider("Lookback (days)", min_value=7, max_value=90, value=30, step=1)
df = load_changelog_entries(days)

if df.empty:
    st.info(
        f"No changelog entries in the last {days} days "
        "(or the corpus isn't reachable from this instance)."
    )
    st.stop()

c1, c2, c3 = st.columns(3)
subsystems = sorted(x for x in df["subsystem"].dropna().unique())
severities = sorted(x for x in df["severity"].dropna().unique())
sources = sorted(x for x in df["source"].dropna().unique())
pick_sub = c1.multiselect("Subsystem", subsystems, default=subsystems)
pick_sev = c2.multiselect("Severity", severities, default=severities)
pick_src = c3.multiselect("Source", sources, default=sources)

view = df[
    df["subsystem"].isin(pick_sub)
    & df["severity"].isin(pick_sev)
    & df["source"].isin(pick_src)
]

if view.empty:
    st.warning("No entries match the current filters.")
    st.stop()

# --- KPI strip --------------------------------------------------------------
k1, k2, k3, k4 = st.columns(4)
k1.metric(f"Incidents ({days}d)", len(view))
last7 = view[view["ts"] >= (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7))]
k2.metric("Last 7d", len(last7))
k3.metric("Critical / High", int(view["severity"].isin(["critical", "high"]).sum()))
k4.metric("Distinct signatures", view["error_signature"].nunique())

# --- Trend + breakdowns -----------------------------------------------------
by_day = (
    view.groupby(["day", "severity"]).size().reset_index(name="count")
    if "day" in view
    else pd.DataFrame()
)
if not by_day.empty:
    st.plotly_chart(
        px.bar(
            by_day, x="day", y="count", color="severity",
            title="Incidents per day", labels={"day": "", "count": "incidents"},
        ),
        use_container_width=True,
    )

b1, b2 = st.columns(2)
by_sub = view.groupby("subsystem").size().reset_index(name="count").sort_values("count")
b1.plotly_chart(
    px.bar(by_sub, x="count", y="subsystem", orientation="h", title="By subsystem"),
    use_container_width=True,
)
by_src = view.groupby("source").size().reset_index(name="count").sort_values("count")
b2.plotly_chart(
    px.bar(by_src, x="count", y="source", orientation="h", title="By feeder/source"),
    use_container_width=True,
)

# --- Top error signatures ---------------------------------------------------
st.subheader("Top error signatures")
sig = (
    view.dropna(subset=["error_signature"])
    .groupby("error_signature")
    .agg(
        count=("event_id", "size"),
        dedup_total=("dedup_count", "sum"),
        subsystems=("subsystem", lambda s: ", ".join(sorted(set(s.dropna())))),
        latest=("ts_utc", "max"),
    )
    .reset_index()
    .sort_values("count", ascending=False)
    .head(20)
)
if sig.empty:
    st.caption("No flow-doctor error signatures in range (CloudWatch-mirror "
               "log-line incidents don't carry one).")
else:
    st.dataframe(sig, use_container_width=True, hide_index=True)

# --- Recent incidents -------------------------------------------------------
st.subheader("Recent incidents")
recent = view[
    ["ts_utc", "severity", "subsystem", "source", "actor", "summary"]
].head(200)
st.dataframe(recent, use_container_width=True, hide_index=True)
