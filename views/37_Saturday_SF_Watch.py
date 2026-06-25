"""
Saturday SF Watch — Alpha Engine (private console)

Operator surface for the autonomous Saturday-SF resilience arc
(spec: nousergon/alpha-engine-config#1227). The
``alpha-engine-saturday-sf-watch-dispatcher`` Lambda fires on a Saturday SF
terminal failure and appends an event to a per-date watch-log at
``s3://alpha-engine-research/consolidated/saturday_sf_watch/{date}.json``
(schema_version, run_date, events: [...]). This page is its consumer surface.

The watch-log is **failure-driven** — a date exists only for a Saturday where
the pipeline failed, so an empty list is the healthy steady state. Today the
watcher runs in **OBSERVE mode** (M1): every event is ``action="observe"`` and
no autonomous fix is enacted. Later milestones populate ``lane`` /
``action`` (proposed / auto-fixed / rerun) and a PR link.

Complementary to **Pipeline Status** (live SF run/succeeded/failed state) and
**Artifact Freshness** (independent artifact-integrity, the Sat→Mon swallow
safeguard) — this page is the failure-event timeline + what-the-watcher-did log.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import (  # noqa: E402
    list_saturday_sf_watch_dates,
    load_saturday_sf_watch,
)

# Terminal-failure status → display color.
_STATUS_COLOR_HEX: dict[str, str] = {
    "FAILED": "#cf222e",
    "TIMED_OUT": "#bf8700",
    "ABORTED": "#82071e",
}

# Watcher action → label. OBSERVE is all M1 emits; the rest are forward-compat
# with M2+ (the agent fills lane/action).
_ACTION_LABEL: dict[str, str] = {
    "observe": "👁 observed",
    "proposed": "📝 proposed (review)",
    "auto_fixed": "🔧 auto-fixed",
    "merged": "🔧 merged",
    "rerun": "🔁 rerun",
}


st.title("📡 Saturday SF Watch")
st.caption(
    "Autonomous Saturday-pipeline resilience watch — failure-event timeline + "
    "what the watcher did. Failure-driven: no entry means no failure. "
    "(config#1227)"
)

dates = list_saturday_sf_watch_dates()

if not dates:
    st.success("✅ No Saturday SF failures recorded.")
    st.caption(
        "The watch-log is written only when the Saturday pipeline fails. An "
        "empty log is the healthy steady state. Live SF status: **Pipeline "
        "Status**; artifact integrity: **Artifact Freshness**."
    )
    st.stop()

col_sel, col_meta = st.columns([1, 3])
with col_sel:
    selected = st.selectbox("Run date", dates, index=0)

data = load_saturday_sf_watch(selected)
if data is None or not data.get("events"):
    st.warning(f"Watch-log for {selected} could not be read or has no events.")
    st.stop()

events = data["events"]
df = pd.DataFrame(events)

# ── Headline tiles ──────────────────────────────────────────────────────────
n_failures = len(events)
distinct_states = sorted({e.get("failed_state") for e in events if e.get("failed_state")})
n_auto = sum(1 for e in events if e.get("action") in ("auto_fixed", "merged"))
n_proposed = sum(1 for e in events if e.get("action") == "proposed")
dispatch_on = any(e.get("agent_dispatch_enabled") for e in events)

tiles = st.columns(4)
with tiles[0]:
    st.metric("Failure events", n_failures)
with tiles[1]:
    st.metric("Distinct failed states", len(distinct_states))
with tiles[2]:
    st.metric("🔧 auto-fixed", n_auto)
with tiles[3]:
    st.metric("📝 proposed", n_proposed)

mode = "agent dispatch ON" if dispatch_on else "OBSERVE only (M1)"
st.caption(
    f"Mode: **{mode}** · updated {data.get('updated_at', '—')} · "
    f"failed state(s): {', '.join(distinct_states) if distinct_states else '—'}"
)

# ── Event timeline table ────────────────────────────────────────────────────
st.subheader("Failure events")

display = pd.DataFrame({
    "Detected": df.get("detected_at"),
    "Status": df.get("status"),
    "Failed state": df.get("failed_state"),
    "Cause": df.get("cause"),
    "Action": df.get("action", pd.Series(["observe"] * len(df))).map(
        lambda a: _ACTION_LABEL.get(a, a or "—")
    ),
    "Lane": df.get("lane"),
    "Execution": df.get("execution_name"),
})


def _status_style(col: pd.Series) -> list[str]:
    return [
        f"background-color: {_STATUS_COLOR_HEX.get(v, '#6e7781')}; color: white"
        for v in col
    ]


st.dataframe(
    display.style.apply(_status_style, subset=["Status"]),
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "Until the agent half lands (M2), every event is observe-only — the watcher "
    "records the failed state + cause but enacts no fix. See config#1227 for the "
    "M2→M5 rollout (propose-only soak → autonomous merge after it earns trust)."
)

with st.expander("Raw watch-log JSON"):
    st.json(data)
