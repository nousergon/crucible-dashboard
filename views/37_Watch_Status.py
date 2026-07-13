"""
Watch Status — Alpha Engine (private console)

Operator surface for BOTH autonomous resilience watches: Saturday SF Watch
(spec: nousergon/alpha-engine-config#1227) and Fleet CI Watch (config#1593 /
#1596). The ``alpha-engine-saturday-sf-watch-dispatcher`` Lambda fires on a
Saturday SF terminal failure and appends an event to a per-date watch-log at
``s3://alpha-engine-research/consolidated/saturday_sf_watch/{date}.json``
(schema_version, run_date, events: [...]); the Fleet CI Watch dispatch mirrors
this shape under ``consolidated/ci_watch/{date}.json`` for main-branch
CI/deploy red events. This page is the consumer surface for both.

Renamed from "Saturday SF Watch" (config#2389) — the page always rendered
both SF Watch and CI Watch sections below; the title/nav label now says so.

Each watch-log is **failure-driven** — a date exists only where the watched
pipeline actually failed/redded, so an empty list is the healthy steady
state. Full autonomy shipped 2026-07-07 (all four dispatch flags now true) —
the watcher dispatches an agent that can propose, auto-fix, merge, and rerun;
``lane`` / ``action`` and a PR link populate per-event once the agent has
run. Events from before 2026-07-07 may still show ``action="observe"`` (the
earlier observe-only milestone) — that's historical, not current-mode.

Complementary to **Pipeline Status** (live SF run/succeeded/failed state) and
**Artifact Freshness** (independent artifact-integrity, the Sat→Mon swallow
safeguard) — this page is the failure-event timeline + what-the-watcher-did log.

Sections, top to bottom (config#2389 aggregate efficacy metrics added above
the pre-existing per-date sections):
0. **Watch Efficacy** — aggregate fix/escalation rates, MTTR, top failure
   modes and canary drill health across ALL watch-log dates
   (``loaders/watch_efficacy.py``).
1. A **Saturday Integrity GO/NO-GO banner** (config#1244) from the independent
   integrity gate's marker (config#1227 §8) — the Sat→Mon swallow safeguard,
   validated independently of the agent's own report.
2. The Saturday SF Watch per-date event timeline, with the watch agent's
   per-event enrichment fields (``pr_urls`` / ``diagnosis`` /
   ``recommended_command``) surfaced (config#1244).
3. The Fleet CI Watch per-date event timeline (config#1593/#1596).
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import (  # noqa: E402
    list_ci_watch_dates,
    list_saturday_integrity_dates,
    list_saturday_sf_watch_dates,
    load_ci_watch,
    load_saturday_integrity,
    load_saturday_sf_watch,
)
from loaders.watch_efficacy import load_watch_efficacy_snapshot  # noqa: E402

# Terminal-failure status → display color.
_STATUS_COLOR_HEX: dict[str, str] = {
    "FAILED": "#cf222e",
    "TIMED_OUT": "#bf8700",
    "ABORTED": "#82071e",
}

# Watcher action → label. "observe" is the pre-2026-07-07 historical
# milestone's only action; full autonomy (shipped 2026-07-07) can also
# emit proposed / auto-fixed / merged / rerun / refused / escalated.
_ACTION_LABEL: dict[str, str] = {
    "observe": "👁 observed",
    "proposed": "📝 proposed (review)",
    "auto_fixed": "🔧 auto-fixed",
    "merged": "🔧 merged",
    "fixed_merged_rerun": "🔧 fixed+merged+rerun",
    "rerun": "🔁 rerun",
    "refused": "🛑 refused",
    "escalated": "🚨 escalated",
}


st.title("📡 Watch Status")
st.caption(
    "Autonomous resilience watch — Saturday SF Watch + Fleet CI Watch. "
    "Failure-event timeline + what the watcher did. Failure-driven: no "
    "entry means no failure. (config#1227 / config#1593)"
)


# ── Watch Efficacy (aggregate metrics across ALL watch-log dates — config#2389) ──
# Sits above the per-date Saturday Integrity banner + event timelines below:
# "how well is the watch actually doing" is the operator's first question,
# the per-date drill-down is the second.
def _render_efficacy_section() -> None:
    st.subheader("📊 Watch Efficacy")
    snap = load_watch_efficacy_snapshot()
    st.caption(f"computed_at: {snap.computed_at.isoformat()}")

    # ── SF Watch ─────────────────────────────────────────────────────────
    st.markdown("**SF Watch**")
    sf = snap.sf_watch
    sf_tiles = st.columns(4)
    sf_tiles[0].metric("Total dispatches", sf.total_events if sf.total_dates else "—")
    sf_tiles[1].metric(
        "Fix rate", f"{sf.fix_rate:.0%}" if sf.total_events else "—"
    )
    sf_tiles[2].metric(
        "Post-autonomy fix",
        f"{sf.post_autonomy_fix_rate:.0%}"
        if sf.post_autonomy_fix_rate is not None else "—",
    )
    sf_tiles[3].metric(
        "MTTR (hrs)", f"{sf.mttr_hours:.1f}" if sf.mttr_hours is not None else "—"
    )
    if sf.top_failure_modes:
        st.caption(
            "Top failure modes: "
            + ", ".join(f"{mode}({n})" for mode, n in sf.top_failure_modes)
        )
    if sf.events_per_date:
        st.dataframe(
            pd.DataFrame(
                sf.events_per_date, columns=["Date", "Events", "Auto-fixed"]
            ),
            use_container_width=True, hide_index=True,
        )

    # ── CI Watch ─────────────────────────────────────────────────────────
    st.markdown("**CI Watch**")
    ci = snap.ci_watch
    ci_tiles = st.columns(3)
    ci_tiles[0].metric("Total dispatches", ci.total_events if ci.total_dates else "—")
    ci_tiles[1].metric(
        "Fix rate", f"{ci.fix_rate:.0%}" if ci.total_events else "—"
    )
    ci_tiles[2].metric(
        "Rerun success rate",
        f"{ci.rerun_success_rate:.0%}" if ci.total_events else "—",
    )
    if ci.per_repo:
        st.dataframe(
            pd.DataFrame(
                sorted(ci.per_repo.items(), key=lambda kv: kv[1], reverse=True),
                columns=["Repo", "Events"],
            ),
            use_container_width=True, hide_index=True,
        )
    if ci.events_per_date:
        st.dataframe(
            pd.DataFrame(
                ci.events_per_date, columns=["Date", "Events", "Auto-fixed"]
            ),
            use_container_width=True, hide_index=True,
        )

    # ── Canary Drill Health ──────────────────────────────────────────────
    st.markdown("**Canary Drill Health**")
    canary = snap.canary
    canary_tiles = st.columns(3)
    canary_tiles[0].metric(
        "SF last drill",
        f"{canary.sf_watch_age_days:.1f}d ago"
        if canary.sf_watch_age_days is not None else "—",
    )
    canary_tiles[1].metric(
        "CI last drill",
        f"{canary.ci_watch_age_days:.1f}d ago"
        if canary.ci_watch_age_days is not None else "—",
    )
    if canary.total_expected_drills:
        canary_tiles[2].metric("Reliability", f"{canary.reliability:.0%}")
    else:
        canary_tiles[2].metric("Reliability", "—")
        st.caption(
            "Canary drills not yet expected — the weekly synthetic-drill "
            "cadence (config#2223) starts 2026-07-23."
        )

    st.divider()


_render_efficacy_section()


def _render_integrity_banner() -> None:
    """Render the Saturday Integrity GO/NO-GO banner from the latest
    ``consolidated/saturday_integrity/{date}.json`` marker (config#1227 §8 —
    the independent Sat→Mon swallow safeguard).

    The marker is the integrity gate's output, **not** the agent's own report:
    GO means every load-bearing Saturday artifact validated fresh/present vs the
    ARTIFACT_REGISTRY before Monday trades; NO-GO names the missing/stale ones.
    Absent-tolerant: until the gate emits its first marker the banner stays
    neutral (mirrors the failure-driven watch-log below).
    """
    idates = list_saturday_integrity_dates()
    if not idates:
        st.info(
            "🛈 Saturday Integrity gate: no marker emitted yet. The independent "
            "Sat→Mon swallow safeguard (config#1227 §8) writes a GO/NO-GO marker "
            "to `consolidated/saturday_integrity/{date}.json` before Monday "
            "trades; this banner activates once the first marker lands."
        )
        return

    marker = load_saturday_integrity(idates[0]) or {}
    # Accept both an explicit "status" GO/NO-GO and a boolean "go" field.
    status = str(marker.get("status", "")).upper()
    go = marker.get("go")
    if go is None:
        go = status in ("GO", "PASS", "OK")
    else:
        go = bool(go)

    # Missing/stale artifacts the gate flagged (tolerate a few field shapes).
    issues = (
        marker.get("missing_or_stale")
        or marker.get("stale_artifacts")
        or marker.get("missing_artifacts")
        or marker.get("issues")
        or []
    )
    when = marker.get("checked_at") or marker.get("updated_at") or idates[0]

    if go:
        st.success(
            f"✅ Saturday Integrity: **GO** — all load-bearing artifacts "
            f"fresh/present ({when})."
        )
    else:
        st.error(f"⛔ Saturday Integrity: **NO-GO** ({when}).")
        if issues:
            st.markdown("**Missing / stale artifacts:**")
            for item in issues:
                if isinstance(item, dict):
                    name = item.get("artifact") or item.get("name") or "—"
                    reason = item.get("reason") or item.get("status") or ""
                    st.markdown(f"- `{name}`" + (f" — {reason}" if reason else ""))
                else:
                    st.markdown(f"- `{item}`")


_render_integrity_banner()

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
n_auto = sum(
    1 for e in events
    if e.get("action") in ("auto_fixed", "merged", "fixed_merged_rerun")
)
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

# Full autonomy shipped 2026-07-07 (all four dispatch flags true); the
# false branch below now means "no dispatch-enabled event in this
# specific date's log" (e.g. a pre-2026-07-07 date), not "OBSERVE-only
# milestone" as a blanket system state.
mode = "agent dispatch ON" if dispatch_on else "agent dispatch off for this date"
st.caption(
    f"Mode: **{mode}** · updated {data.get('updated_at', '—')} · "
    f"failed state(s): {', '.join(distinct_states) if distinct_states else '—'}"
)

# ── Event timeline table ────────────────────────────────────────────────────
st.subheader("Failure events")


def _count_prs(v: object) -> str:
    """Render the pr_urls list as a compact count (the links live in the detail
    expander below — a dataframe cell can't hold clickable links)."""
    if isinstance(v, (list, tuple)) and v:
        return f"🔗 {len(v)}"
    return "—"


display = pd.DataFrame({
    "Detected": df.get("detected_at"),
    "Status": df.get("status"),
    "Failed state": df.get("failed_state"),
    "Cause": df.get("cause"),
    "Action": df.get("action", pd.Series(["observe"] * len(df))).map(
        lambda a: _ACTION_LABEL.get(a, a or "—")
    ),
    "Lane": df.get("lane"),
    "Diagnosis": df.get("diagnosis"),
    "PRs": (df["pr_urls"].map(_count_prs) if "pr_urls" in df else "—"),
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
    "Full autonomy shipped 2026-07-07 (config#1227's M2→M5 rollout — "
    "propose-only soak → autonomous merge — complete; all four dispatch "
    "flags now true). Events dated before 2026-07-07 may show "
    "observe-only action from the earlier milestone; that's historical."
)

# ── Agent action detail (enrichment fields written by the watch agent) ───────
# The watch agent enriches each event (schema_version 2) with pr_urls (links),
# diagnosis (root cause) and recommended_command (when it stopped short). The
# dataframe above can only summarize these; this section surfaces them in full.
_enriched = [
    e for e in events
    if e.get("pr_urls") or e.get("diagnosis") or e.get("recommended_command")
]
if _enriched:
    st.subheader("Agent action detail")
    for e in _enriched:
        label = (
            e.get("failed_state")
            or e.get("execution_name")
            or e.get("detected_at")
            or "event"
        )
        action_label = _ACTION_LABEL.get(e.get("action"), e.get("action") or "observe")
        with st.expander(f"🔧 {label} — {action_label}"):
            if e.get("diagnosis"):
                st.markdown(f"**Diagnosis:** {e['diagnosis']}")
            pr_urls = e.get("pr_urls") or []
            if pr_urls:
                st.markdown("**PRs:**")
                for url in pr_urls:
                    st.markdown(f"- [{url}]({url})")
            if e.get("recommended_command"):
                st.markdown("**Recommended command** (agent stopped short):")
                st.code(e["recommended_command"], language="bash")
            if e.get("rerun_execution_arn"):
                st.caption(f"Rerun execution: `{e['rerun_execution_arn']}`")

with st.expander("Raw watch-log JSON"):
    st.json(data)


# ── Fleet CI Watch (main-branch CI/deploy red events — config#1593/#1596) ────
# Distinct schema from the Saturday SF watch-log above: repo/run_id-keyed CI
# runs, not a pipeline execution_arn — rendered with its own row shape rather
# than reusing the SF event table.
def _render_fleet_ci_watch() -> None:
    st.divider()
    st.subheader("📡 Fleet CI Watch")
    st.caption(
        "Main-branch CI/deploy red events across the fleet — dispatched to the "
        "watch agent, which diagnoses + (where possible) fixes. Failure-driven: "
        "a date exists only where a dispatch actually fired. (config#1593)"
    )

    ci_dates = list_ci_watch_dates()
    if not ci_dates:
        st.info(
            "🛈 No Fleet CI Watch events recorded yet. A date lands here on the "
            "first main-branch CI/deploy red dispatch."
        )
        return

    ci_selected = st.selectbox("CI Watch date", ci_dates, index=0, key="ci_watch_date")
    ci_data = load_ci_watch(ci_selected)
    if ci_data is None or not ci_data.get("events"):
        st.warning(f"CI Watch log for {ci_selected} could not be read or has no events.")
        return

    ci_events = ci_data["events"]
    ci_df = pd.DataFrame(ci_events)

    ci_tiles = st.columns(3)
    with ci_tiles[0]:
        st.metric("CI events", len(ci_events))
    with ci_tiles[1]:
        st.metric("Distinct repos", ci_df["repo"].nunique() if "repo" in ci_df else 0)
    with ci_tiles[2]:
        n_followup = sum(1 for e in ci_events if e.get("followup_issues"))
        st.metric("With followup issues", n_followup)

    ci_display = pd.DataFrame({
        "Repo": ci_df.get("repo"),
        "Workflow": ci_df.get("workflow"),
        "SHA": ci_df.get("sha", pd.Series(dtype=str)).map(
            lambda s: (s or "")[:8] if isinstance(s, str) else s
        ),
        "Lane": ci_df.get("lane"),
        "Action": ci_df.get("action", pd.Series(["observe"] * len(ci_df))).map(
            lambda a: _ACTION_LABEL.get(a, a or "—")
        ),
        "Attempt": ci_df.get("agent_attempt"),
        "Diagnosis": ci_df.get("diagnosis"),
        "PRs": (ci_df["pr_urls"].map(_count_prs) if "pr_urls" in ci_df else "—"),
    })
    st.dataframe(ci_display, use_container_width=True, hide_index=True)

    for e in ci_events:
        label = e.get("workflow") or e.get("repo") or "event"
        run_url = e.get("run_url")
        with st.expander(f"🔧 {e.get('repo', '—')} · {label}"):
            if run_url:
                st.markdown(f"**Run:** [{run_url}]({run_url}) (run_id `{e.get('run_id', '—')}`)")
            elif e.get("run_id"):
                st.caption(f"Run ID: `{e['run_id']}`")
            if e.get("sha"):
                st.caption(f"SHA: `{e['sha']}`")
            if e.get("diagnosis"):
                st.markdown(f"**Diagnosis:** {e['diagnosis']}")
            if e.get("rerun_conclusion"):
                st.markdown(f"**Rerun conclusion:** {e['rerun_conclusion']}")
            pr_urls = e.get("pr_urls") or []
            if pr_urls:
                st.markdown("**PRs:**")
                for url in pr_urls:
                    st.markdown(f"- [{url}]({url})")
            followups = e.get("followup_issues") or []
            if followups:
                st.markdown("**Followup issues:**")
                for fu in followups:
                    st.markdown(f"- {fu}")

    with st.expander("Raw CI Watch JSON"):
        st.json(ci_data)


_render_fleet_ci_watch()
