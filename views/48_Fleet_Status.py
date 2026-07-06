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
Freshness, Backlog Groom) — this page is the triage index, not a rebuild
of those surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from fleet_status import GROUP_ORDER, ComponentStatus, resolve_fleet
from loaders.fleet_status_loader import gather_fleet_inputs

st.title("🛰 Fleet Status")
st.caption(
    "Live status of every fleet component — auto-refreshes every 30 s. "
    "🟢 online/healthy · 🟡 expected but stalled · 🔴 expected and offline/failed · "
    "⚪ not expected right now."
)


def _render_row(s: ComponentStatus) -> None:
    c_dot, c_reason, c_when = st.columns([3, 5, 2])
    with c_dot:
        if s.deep_link:
            st.page_link(f"views/{_PAGE_BY_SLUG[s.deep_link]}",
                         label=f"{s.icon} **{s.label}**")
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


# Deep-link slugs → view scripts (st.page_link wants the script path; slugs
# here are the pinned url_paths of the standalone pages plus host-tab pages).
_PAGE_BY_SLUG = {
    "pipeline-status": "25_Pipeline_Status.py",
    "artifact-freshness": "26_Artifact_Freshness.py",
    "backlog-groom": "42_Backlog_Groom.py",
}


@st.fragment(run_every="30s")
def _live_grid() -> None:
    inputs = gather_fleet_inputs()
    statuses = resolve_fleet(inputs)

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
