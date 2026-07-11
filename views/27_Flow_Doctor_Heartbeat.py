"""
Flow-Doctor Heartbeat — Alpha Engine (private console)

The fleet's flow-doctor liveness at a glance — the System Health consumer for
the "make it actually kick in" arc (config#646, step 2). Each producing flow's
entrypoint calls ``FlowDoctor.emit_heartbeat()`` at end-of-run, landing its
``status()`` snapshot at
``s3://alpha-engine-research/_flow_doctor/heartbeat/{flow}/{date}.json``. This
page reads those snapshots and answers the two questions the log-level
``decision_reason`` / ``log_summary()`` lines couldn't at a glance:

  - **alive but quiet** — flow-doctor is running and saw no errors worth acting
    on (healthy, zero fired); vs
  - **suppressing X** — it saw errors but suppressed them (rate-limited, category
    /severity-filtered, deduped, no-notifiers), which can hide a real problem.

Composes with the Artifact Freshness page pattern (``26_Artifact_Freshness.py``)
— a plain S3-JSON read, no LLM call, no cost. Hosted on the System Health
(Agent Fleet) surface.
"""
from __future__ import annotations

import os
import sys
from datetime import date as _date
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import (
    list_flow_doctor_heartbeat_flows,
    load_flow_doctor_heartbeat_latest,
)

# The suppression buckets in status()["decisions_today"] — everything that is
# NOT a FIRED decision. A high count here with zero fired is the "suppressing X"
# state the operator needs to see (config#646).
_SUPPRESS_KEYS = (
    "SEVERITY_FILTERED",
    "CATEGORY_FILTERED",
    "RATE_LIMITED",
    "DELIVERY_FAILED",
    "NO_NOTIFIERS",
    "DEDUPED",
)


def _age_hours(iso_ts) -> float | None:
    """Hours since an ISO-8601 UTC timestamp, or None if unparseable."""
    if not isinstance(iso_ts, str) or not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def _classify(hb: dict) -> tuple[str, str]:
    """Return (emoji_label, one-line status) for a heartbeat payload.

    'stale' when the newest heartbeat's date is not today (the flow didn't run
    today); otherwise 'suppressing' when it suppressed ≥1 error with zero fired,
    'firing' when it fired ≥1, else 'alive but quiet'. Reads defensively — a
    flow may emit before this schema stabilizes.
    """
    status = hb.get("status") or {}
    decisions = status.get("decisions_today") or {}
    fired = int(decisions.get("FIRED", 0) or 0)
    suppressed = sum(int(decisions.get(k, 0) or 0) for k in _SUPPRESS_KEYS)
    errors_seen = int(status.get("errors_seen_today", 0) or 0)

    hb_date = str(hb.get("ts_utc", ""))[:10]
    today = _date.today().isoformat()
    if hb_date and hb_date != today:
        return "🌙 stale", f"last heartbeat {hb_date} (no run today)"
    if not status.get("healthy", True):
        return "🚨 unhealthy", f"flow-doctor reports unhealthy — {errors_seen} error(s) seen"
    if fired:
        return "🔔 firing", f"fired {fired} · suppressed {suppressed} · {errors_seen} error(s) seen"
    if suppressed:
        return "🔇 suppressing", f"suppressed {suppressed} error(s), fired 0 — check why"
    return "✅ alive (quiet)", f"healthy · {errors_seen} error(s) seen, none actionable"


st.markdown("### 🩺 Flow-Doctor Heartbeat")
st.caption(
    "Per-flow end-of-run liveness from `emit_heartbeat()` "
    "(`s3://alpha-engine-research/_flow_doctor/heartbeat/{flow}/{date}.json`). "
    "Answers 'alive but quiet' vs 'suppressing X per flow' — config#646. "
    "Read-only, no LLM call."
)

flows = list_flow_doctor_heartbeat_flows()
if not flows:
    st.info(
        "No flow-doctor heartbeats found under "
        "`_flow_doctor/heartbeat/` yet. Each producing flow's entrypoint emits "
        "one at end-of-run once the `emit_heartbeat()` wiring (config#646) has "
        "landed and run."
    )
    st.stop()

rows: list[dict] = []
for flow in flows:
    hb = load_flow_doctor_heartbeat_latest(flow)
    if not hb:
        rows.append({"flow": flow, "state": "— no heartbeat", "detail": "",
                     "fired": None, "suppressed": None, "errors_seen": None,
                     "last_heartbeat": "—", "age (h)": None})
        continue
    status = hb.get("status") or {}
    decisions = status.get("decisions_today") or {}
    label, detail = _classify(hb)
    age = _age_hours(hb.get("ts_utc"))
    rows.append({
        "flow": flow,
        "state": label,
        "detail": detail,
        "fired": int(decisions.get("FIRED", 0) or 0),
        "suppressed": sum(int(decisions.get(k, 0) or 0) for k in _SUPPRESS_KEYS),
        "errors_seen": int(status.get("errors_seen_today", 0) or 0),
        "last_heartbeat": str(hb.get("ts_utc", "—")),
        "age (h)": round(age, 1) if age is not None else None,
    })

df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)

# Surface the actionable states up top: anything suppressing/unhealthy/stale.
attention = [r for r in rows
             if any(s in r["state"] for s in ("suppressing", "unhealthy", "stale"))
             or r["state"].startswith("— no")]
if attention:
    st.warning(
        "Needs a look: "
        + ", ".join(f"**{r['flow']}** ({r['state']})" for r in attention)
    )
else:
    st.success("All flows alive — heartbeats fresh, nothing silently suppressed.")

st.caption(
    "`fired` = alerts flow-doctor actually raised; `suppressed` = errors it saw "
    "but held back (rate-limited / category- or severity-filtered / deduped / "
    "no-notifiers). A high `suppressed` with `fired` 0 can hide a real problem — "
    "the exact 'is it alive or just quiet?' question this page exists to answer."
)
