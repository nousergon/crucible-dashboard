"""
48_Fleet_Status.py — real-time fleet status grid (System & Ops).

One glance = the whole system: every component that runs in a week, each
with a schedule-aware dot. The grid lives in a ``st.fragment`` that
re-polls every 30 s — no manual refresh — and each tick lands on fresh
loader reads (25 s cache TTL).

  🟢 online / running / last cycle complete within SLA
  🟡 should be live right now but stalled (grace, stale heartbeat,
     SSM ping lost, overdue scheduled start)
  🔴 expected and offline / failed / missing past SLA
  ⚪ not expected right now (off-hours / weekend / holiday) or no probe

Status semantics + the planes composed live in ``fleet_status.py`` (pure,
frozen-clock-tested); input gathering in ``loaders/fleet_status_loader.py``.
Rows deep-link into the existing detail pages (Pipeline Status, Artifact
Freshness, Backlog Groom, Watch Status) — this page is the triage
index, not a rebuild of those surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from fleet_status import (
    GROUP_ORDER,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    WEEKLY_SF_STRIP_PLUMBING_STATES,
    ComponentStatus,
    build_weekly_sf_strip,
    resolve_fleet,
)
from loaders.fleet_status_loader import gather_fleet_inputs, rag_ingestion_progress
from loaders.s3_loader import load_intraday_heartbeat, load_intraday_latest_prices

st.title("🛰 Fleet Status")
st.caption(
    "Live status of every fleet component — auto-refreshes every 30 s. "
    "🟢 online/healthy · 🟡 expected but stalled · 🔴 expected and offline/failed · "
    "⚪ not expected right now. Watch-agent rows additionally escalate on a "
    "missed weekly canary drill of the dispatch pipe (config#2223) — the "
    "signal that catches a silently broken dispatch between real failures."
)


# Deep-link slugs → console URL paths. st.page_link is NOT usable here:
# it only accepts pages registered in st.navigation, and post nav-collapse
# (dashboard#273) Artifact Freshness / Backlog Groom are view-host TABS
# (shared/view_host.py, ?tab=<label> round-trip), not registered pages —
# page_link raised StreamlitPageNotFoundError in production (2026-07-06).
# Markdown page links navigate fine for both registered slugs and
# host-page?tab= URLs. Targets guarded by
# tests/test_fleet_status_page.py::TestDeepLinkTargets.
_URL_BY_SLUG = {
    "pipeline-status": "pipeline-status",  # standalone st.Page, pinned slug
    "artifact-freshness": "host_observability?tab=Artifact+Freshness",
    "backlog-groom": "host_system_health?tab=Backlog+Groom",
    "saturday-sf-watch": "host_system_health?tab=Watch+Status",
}


def _render_row(s: ComponentStatus) -> None:
    c_dot, c_reason, c_when = st.columns([3, 5, 2])
    with c_dot:
        url = _URL_BY_SLUG.get(s.deep_link) if s.deep_link else None
        if url:
            st.markdown(f"[{s.icon} **{s.label}**](/{url})")
        else:
            st.markdown(f"{s.icon} **{s.label}**")
    with c_reason:
        st.markdown(s.reason)
    with c_when:
        if s.last_activity_utc is not None:
            st.caption(s.last_activity_utc.strftime("%a %H:%M:%S UTC"))
    if s.detail:
        with st.expander(f"{s.label} — detail ({len(s.detail)} rows)"):
            st.dataframe(
                pd.DataFrame(list(s.detail)),
                use_container_width=True, hide_index=True,
            )


# ── Weekly-SF live progress strip (config-I2966) ────────────────────────────
# Renders ONLY while a weekly-SF execution is RUNNING (per the issue's "no
# dead chrome" acceptance criterion) — absent entirely the rest of the week.
# Reuses the SAME registry-backed state list page 25 renders
# (fleet_status.build_weekly_sf_strip / WEEKLY_SF_STRIP_STATES) rather than
# hand-maintaining a second state list; the drift guard
# (tests/test_pipeline_status_registry_drift.py::
# test_weekly_sf_strip_states_are_all_registered) keeps it honest against
# the live SF JSON.

_STEP_MARKDOWN = {
    STEP_DONE: "✅",
    STEP_RUNNING: "🚀",
    STEP_PENDING: "⚪",
    STEP_FAILED: "🔴",
}


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return ""
    secs = max(0, int(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _render_step_chip(step) -> None:
    icon = _STEP_MARKDOWN.get(step.state, "❓")
    label = step.state_name
    suffix = ""
    if step.state == STEP_RUNNING and step.elapsed_sec is not None:
        suffix = f" ({_format_elapsed(step.elapsed_sec)})"
    st.markdown(f"{icon} **{label}**{suffix}")
    if step.rag_inner_step:
        amber = " 🟠" if step.rag_stale else ""
        st.caption(f"{step.rag_inner_step}{amber}")


def _render_weekly_sf_strip(inputs) -> None:
    """Render the compact horizontal step strip for the weekly SF's CURRENT
    execution, iff it is RUNNING right now. Absent (renders nothing) for
    every other pipeline state — idle, succeeded, failed, no-executions,
    unavailable — per the issue's explicit "no dead chrome" requirement;
    page 25 remains the durable detail table for those states.

    Renders the 9 substantive PRODUCTION stages (MorningEnrich ... Director,
    plus the two ResearchPredictorParallel lanes) as the main visual strip;
    the pre-spend gates + terminal notify/degraded-alert plumbing
    (WEEKLY_SF_STRIP_PLUMBING_STATES — still part of the SAME registry-
    backed, drift-guarded state list, just visually de-emphasized) render
    compactly under an expander so the operator's eye goes straight to the
    stages that actually take wall-clock time.
    """
    snap = inputs.pipelines.get("weekly")
    if snap is None or snap.status != "RUNNING":
        return

    now = inputs.now
    run_date = (snap.started_at or now).strftime("%Y-%m-%d")
    rag_progress = rag_ingestion_progress(run_date)
    steps = build_weekly_sf_strip(snap.tasks, now=now, rag_progress=rag_progress)

    st.subheader("🏃 Weekly SF — live progress")
    st.caption(
        "ne-weekly-freshness-pipeline is RUNNING — step strip below "
        "(done ✅ / running 🚀 / pending ⚪ / failed 🔴). "
        "[Full detail table → Pipeline Status](/pipeline-status)"
    )

    production = [s for s in steps if s.state_name not in WEEKLY_SF_STRIP_PLUMBING_STATES]
    plumbing = [s for s in steps if s.state_name in WEEKLY_SF_STRIP_PLUMBING_STATES]

    linear = [s for s in production if s.lane is None]
    branch_a = [s for s in production if s.lane == "Branch A"]
    branch_b = [s for s in production if s.lane == "Branch B"]

    pre_parallel = [s for s in linear if s.state_name in ("MorningEnrich", "DataPhase1")]
    post_parallel = [s for s in linear if s not in pre_parallel]

    if pre_parallel:
        cols = st.columns(len(pre_parallel))
        for col, step in zip(cols, pre_parallel):
            with col:
                _render_step_chip(step)

    if branch_a or branch_b:
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Branch A** (Research)")
            for step in branch_a:
                _render_step_chip(step)
        with col_b:
            st.markdown("**Branch B** (PredictorTraining)")
            for step in branch_b:
                _render_step_chip(step)

    if post_parallel:
        cols = st.columns(len(post_parallel))
        for col, step in zip(cols, post_parallel):
            with col:
                _render_step_chip(step)

    if plumbing:
        done_ct = sum(1 for s in plumbing if s.state == STEP_DONE)
        with st.expander(
            f"Gates + terminal notifiers ({done_ct}/{len(plumbing)} done)",
            expanded=False,
        ):
            cols = st.columns(min(len(plumbing), 6) or 1)
            for i, step in enumerate(plumbing):
                with cols[i % len(cols)]:
                    _render_step_chip(step)

    st.divider()


@st.fragment(run_every="30s")
def _live_grid() -> None:
    inputs = gather_fleet_inputs()
    statuses = resolve_fleet(inputs)

    # config-I2966: strip renders ONLY while the weekly SF is RUNNING —
    # absent (no-op) otherwise. Ahead of the KPI row so it's the first
    # thing an operator sees during the one window it matters (a live
    # Saturday/shell-run) without permanently taking space above the fold.
    _render_weekly_sf_strip(inputs)

    # Degraded-plane banners — named, loud, per feedback_no_silent_fails.
    if not inputs.ec2_available:
        st.warning(
            "EC2/SSM control-plane reads unavailable — instance rows are "
            f"degraded. Cause: {inputs.ec2_error}. If this is an "
            "AccessDenied, the `alpha-engine-dashboard-fleet-liveness` IAM "
            "policy has not been applied to the dashboard role "
            "(alpha-engine-config `iam/alpha-engine-dashboard-role/`).",
            icon="⚠️",
        )

    counts = {"green": 0, "yellow": 0, "red": 0, "gray": 0}
    for s in statuses:
        counts[s.dot] = counts.get(s.dot, 0) + 1
    kpi = st.columns(5)
    kpi[0].metric("🟢 Online", counts["green"])
    kpi[1].metric("🟡 Stalled", counts["yellow"])
    kpi[2].metric("🔴 Offline", counts["red"])
    kpi[3].metric("⚪ Idle/N-A", counts["gray"])
    kpi[4].metric(
        "Polled",
        datetime.now(timezone.utc).strftime("%H:%M:%S"),
        delta="UTC · every 30s",
        delta_color="off",
    )

    for group in GROUP_ORDER:
        rows = [s for s in statuses if s.group == group]
        if not rows:
            continue
        st.divider()
        st.subheader(group)
        for s in rows:
            _render_row(s)

    if not inputs.is_trading_day:
        st.caption(
            "Today is not an NYSE trading day — trading-window components "
            "show ⚪ by design."
        )


_live_grid()

st.caption(
    "Substrate: AWS EC2/SSM control plane + Step Functions "
    "(`loaders.pipeline_status_loader`, 60s cache) + S3 artifacts "
    "(freshness-monitor heartbeat, groom markers, `health/*.json` "
    "self-reports) + local `systemctl` probe, composed by "
    "`fleet_status.resolve_fleet()`. Loader cache: 25s "
    "(`loaders.fleet_status_loader._TTL_SECONDS`) — the page's own "
    "`st.fragment(run_every=\"30s\")` tick always lands on a fresh read."
)

# ── Raw daemon snapshots ────────────────────────────────────────────────────
# Rehomed from the retired Intraday Surveillance page (console-IA phase 2a,
# config#1987): the raw surveillance snapshots the every-30-min alerts Lambda
# consumes each run. The alerts process itself persists no per-run artifact
# (Telegram-only); the live NAV strip/curve lives on the public live page.
st.divider()
with st.expander("Raw daemon snapshots — heartbeat + latest IB prices", expanded=False):
    _heartbeat = load_intraday_heartbeat()
    _prices = load_intraday_latest_prices()

    st.markdown("**Daemon heartbeat** — `intraday/heartbeat.json`")
    if _heartbeat:
        st.json(_heartbeat)
    else:
        st.info(
            "No heartbeat snapshot available — the daemon publishes this "
            "during market hours; expected empty outside the trading window "
            "or pre-deploy."
        )

    st.markdown("**Latest IB snapshot prices** — `intraday/latest_prices.json`")
    if _prices:
        st.json(_prices)
    else:
        st.info(
            "No latest-prices snapshot available — daemon-published during "
            "market hours."
        )
