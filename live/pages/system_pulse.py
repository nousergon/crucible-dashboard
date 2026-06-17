"""System Pulse — process telemetry from the running system (ROADMAP L4570e).

The live dashboard's second page (plan §9b.2): proof-of-life evidence that
the pipelines run, the artifacts stay fresh, and the research cycle turns —
shown strictly as PROCESS datapoints. No returns, alpha, grades, accuracy,
or any other outcome figure appears here (public-presence disclosure line);
that tier stays on the gated console.

Absorbs the standalone Uptime page as a status strip (the uptime substrate
and renderer are reused unchanged).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import streamlit as st

from components.uptime_kpi import render_uptime_kpi
from loaders.s3_loader import load_uptime_history
from loaders.system_pulse_loader import (
    SATURDAY_ARN,
    WEEKDAY_ARN,
    load_freshness_summary,
    load_latest_cost_summary,
    load_pipeline_run,
    load_research_activity,
)

_UPTIME_WINDOW_SESSIONS = 20

_STATUS_EMOJI = {
    "SUCCEEDED": "✅",
    "RUNNING": "🚀",
    "FAILED": "🔴",
    "TIMED_OUT": "⏰",
    "ABORTED": "⛔",
    "SKIPPED": "⏭️",
    "NOT_RUN": "·",
    "NOT-RUN": "·",
}

# A cycle's headline verdict is judged by artifacts produced, not the Step
# Function's terminal exit code (config#727 / #856) — see
# loaders.system_pulse_loader.derive_cycle_verdict.
_VERDICT_EMOJI = {
    "COMPLETE": "✅",
    "PARTIAL": "⚠️",
    "FAILED": "🔴",
    "RUNNING": "🚀",
    "NOT_RUN": "·",
}
_VERDICT_LABEL = {
    "COMPLETE": "Complete",
    "PARTIAL": "Partial",
    "FAILED": "Failed",
    "RUNNING": "Running",
    "NOT_RUN": "Not run",
}


def _fmt_status(status: str) -> str:
    return f"{_STATUS_EMOJI.get(status, '·')} {status.replace('_', ' ').replace('-', ' ').title()}"


def _fmt_verdict(verdict: str) -> str:
    emoji = _VERDICT_EMOJI.get(verdict, "·")
    label = _VERDICT_LABEL.get(verdict, verdict.replace("_", " ").title())
    return f"{emoji} {label}"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _fmt_when(iso_ts: str | None) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_ts


def _render_cycle(title: str, caption: str, run: dict | None) -> None:
    st.markdown(f"### {title}")
    if run is None:
        st.caption(caption)
        st.info("Pipeline status temporarily unavailable.")
        return
    verdict = run.get("verdict", "")
    dag_status = run.get("status", "")
    n_total = run.get("artifacts_total") or 0
    headline = (
        f"{caption} · Last run {_fmt_when(run.get('start_utc'))} — "
        f"{_fmt_verdict(verdict)}"
    )
    if n_total:
        headline += f" · {run.get('artifacts_produced', 0)}/{n_total} artifacts produced"
    st.caption(headline)
    # Transparency: never hide a genuine SF failure. When the run produced
    # its artifacts but the Step Function still exited non-OK (a Catch /
    # DataLimitExceeded / terminal-notify step), say so plainly — the cycle
    # succeeded; the plumbing tripped at a non-artifact step.
    if verdict == "COMPLETE" and dag_status not in ("SUCCEEDED", "RUNNING", ""):
        st.caption(
            f"↳ Step Function reported {_fmt_status(dag_status)} at a "
            "non-artifact step; all tracked artifacts were produced."
        )
    tasks = run.get("tasks") or []
    if not tasks:
        st.info("No step detail available for this run.")
        return
    df = pd.DataFrame(
        {
            "Step": [t["name"] for t in tasks],
            "Status": [_fmt_status(t["status"]) for t in tasks],
            "Started": [_fmt_when(t["start_utc"]) for t in tasks],
            "Duration": [_fmt_duration(t["duration_sec"]) for t in tasks],
        }
    )
    st.dataframe(df, hide_index=True, width="stretch")


st.title("System Pulse")
st.caption(
    "Process telemetry from the running system — pipeline cycles, artifact "
    "freshness, research activity, cost. Performance data does not live on "
    "this surface."
)

# ── Pipeline cycles ─────────────────────────────────────────────────────

st.caption(
    "Cycle health reflects whether the run produced its artifacts, not the "
    "Step Function's terminal exit code — a run can write every artifact yet "
    "exit non-OK on a downstream notification step."
)

_render_cycle(
    "Weekly cycle",
    "Saturday research / training / evaluation pipeline.",
    load_pipeline_run(SATURDAY_ARN, "weekly"),
)

_render_cycle(
    "Daily cycle",
    "Weekday morning data → inference → planning → execution pipeline.",
    load_pipeline_run(WEEKDAY_ARN, "daily"),
)

st.divider()

# ── Freshness / activity / cost tiles ───────────────────────────────────

c1, c2, c3 = st.columns(3)

with c1:
    st.markdown("#### Artifact freshness")
    fresh = load_freshness_summary()
    if fresh is None:
        st.info("Freshness data temporarily unavailable.")
    else:
        st.metric(
            "Within SLA",
            f"{fresh['within_sla']} / {fresh['n_total']}",
            help=(
                "Load-bearing pipeline artifacts checked against a declarative "
                "SLA registry every 15 minutes — a missing artifact is itself "
                "an alarm."
            ),
        )
        breakdown = []
        if fresh["stale"]:
            breakdown.append(f"{fresh['stale']} stale")
        if fresh["missing"]:
            breakdown.append(f"{fresh['missing']} missing")
        if fresh["probe_failed"]:
            breakdown.append(f"{fresh['probe_failed']} probe-failed")
        st.caption(
            (", ".join(breakdown) if breakdown else "all artifacts within SLA")
            + f" · checked {_fmt_when(fresh.get('last_run'))}"
        )

with c2:
    st.markdown("#### Research activity")
    act = load_research_activity()
    if act is None:
        st.info("Research activity temporarily unavailable.")
    else:
        st.metric(
            "Tracked names",
            act["tracked"],
            help="Rolling research population maintained by the weekly multi-agent cycle.",
        )
        st.caption(
            f"{act['population']} in population · {act['buy_candidates']} buy "
            f"candidates · regime: {act['regime'] or '—'} · cycle {act['date'] or '—'}"
        )

with c3:
    st.markdown("#### LLM cost")
    cost = load_latest_cost_summary()
    if cost is None:
        st.info("Cost telemetry temporarily unavailable.")
    else:
        st.metric(
            "Last research cycle",
            f"${cost['total_usd']:,.2f}",
            help=(
                "Per-call cost telemetry captured at LLM call time and "
                "aggregated per weekly research run."
            ),
        )
        st.caption(f"{cost['n_calls']:,} LLM calls · captured {cost['capture_date']}")

st.divider()

# ── Uptime strip (absorbs the standalone Uptime page) ───────────────────

st.markdown("### Reliability")
st.caption(
    "Pipeline reliability — \"is the system running?\" — across the most "
    "recent trading sessions."
)
render_uptime_kpi(load_uptime_history(max_sessions=_UPTIME_WINDOW_SESSIONS))
