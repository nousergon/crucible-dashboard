"""
25_Pipeline_Status.py — Operator console for Step Function execution state.

Phase 2 of the pipeline-reporting-revamp arc (ROADMAP L3050, plan doc
``~/Development/alpha-engine-docs/private/pipeline-reporting-revamp-260524.md``).
Phase 1 (lib substrate `nousergon_lib.pipeline_status` v0.28.1) merged
2026-05-24 via alpha-engine-lib PR #60.

What this page shows
====================

Three sections — Weekly Freshness / Pre-open Trading / Post-close Trading —
each backed by the most-recent execution of that state machine via
``read_pipeline_state``.
For each pipeline:

- **Header**: pretty label + run status + duration + start/stop UTC
- **Banner**: green (live ≤ 60s) / yellow (cache fallback, age annotated)
  / red (no cache available, error message named)
- **State table**: one row per substantive Task step (Wait companions
  rolled up into parents per plan doc §3.2), columns
  ``[State, Status, Start UTC, Duration, Latest output]``
- **Latest output cell**: either a deep-link to the matching artifact-archive
  page OR an explicit non-generic reason string from the lib registry —
  never "no artifact" placeholders per ``feedback_no_silent_fails``.

What this page does NOT show
=============================

- RUNNING-state streaming live updates (30-60s poll latency is acceptable
  for the operational glance pattern; sub-second updates are streaming
  territory and out of scope).
- Historical executions beyond the most-recent one — page 25 is "current
  state of each SF"; deep historical drill-down lives in the SF console
  via the "History" deep-link footer.
- Replacement of the per-process archive pages (16-22) — those remain the
  authoritative artifact surface; this page is the navigation router that
  points to them in one consolidated table.

Substrate dependencies
======================

- ``nousergon_lib.pipeline_status`` v0.28.1 (lib PR #60 merged 5/24)
- ``alpha-engine-dashboard-sfn-read`` IAM policy on
  ``alpha-engine-executor-role`` (alpha-engine PR #206)
- ``s3://alpha-engine-research/dashboard/pipeline_status_cache.json``
  last-good cache (written every successful refresh; read on fallback)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from nousergon_lib.pipeline_status import (
    PIPELINE_LABELS,
    PipelineExecutionSummary,
    PipelineRun,
    RunStatus,
    TaskStatus,
)
from nousergon_lib.pipeline_status.registry import ArchivePageRef, ArtifactReason
from loaders.pipeline_status_loader import (
    LoadOutcome,
    LoadResult,
    derive_cycle_verdict,
    list_recent_pipeline_runs_for_arn,
    read_pipeline_state_with_fallback,
    refresh_and_write_cache,
)


_REGION = "us-east-1"
_ACCOUNT_ID = "711398986525"


def _arn_for(sf_name: str) -> str:
    return f"arn:aws:states:{_REGION}:{_ACCOUNT_ID}:stateMachine:{sf_name}"


# Stable order: Weekly Freshness first (it's the headline weekly run), then
# Pre-open Trading (daily cadence), then Post-close Trading (post-market
# reconciliation).
_SF_ORDER: list[str] = [
    "ne-weekly-freshness-pipeline",
    "ne-preopen-trading-pipeline",
    "ne-postclose-trading-pipeline",
]
_ALL_ARNS: list[str] = [_arn_for(n) for n in _SF_ORDER]


# Canonical pipeline_role per SF (Option-D 2026-05-25). The default page
# render filters to these so smoke / recovery / operator-replay
# executions don't displace the cadence run as "most recent." Mirrors
# the alpha-engine-data EventBridge cron rules + alpha-engine daemon
# _trigger_eod_pipeline tag values.
_CANONICAL_ROLE_BY_SF: dict[str, str] = {
    "ne-weekly-freshness-pipeline": "weekly",
    "ne-preopen-trading-pipeline": "daily",
    "ne-postclose-trading-pipeline": "eod",
}


def _canonical_role_for(arn: str) -> Optional[str]:
    sm_name = arn.rsplit(":", 1)[-1]
    return _CANONICAL_ROLE_BY_SF.get(sm_name)


def _role_badge(role: Optional[str]) -> str:
    """Render the role tag as a small markdown badge for the section header."""
    if not role:
        return "`role: unknown`"
    return f"`role: {role}`"


_RUN_STATUS_EMOJI = {
    RunStatus.RUNNING: "🚀",
    RunStatus.SUCCEEDED: "✅",
    RunStatus.FAILED: "🔴",
    RunStatus.TIMED_OUT: "⏰",
    RunStatus.ABORTED: "⛔",
    RunStatus.NOT_RUN: "—",
}


# Artifact-completion verdict (config#727 / #856) — judged by artifacts
# produced, not the SF terminal RunStatus. See
# loaders.pipeline_status_loader.derive_cycle_verdict.
_VERDICT_EMOJI = {
    "COMPLETE": "✅",
    "PARTIAL": "⚠️",
    "FAILED": "🔴",
    "RUNNING": "🚀",
    "NOT_RUN": "—",
}
_VERDICT_LABEL = {
    "COMPLETE": "Complete",
    "PARTIAL": "Partial",
    "FAILED": "Failed",
    "RUNNING": "Running",
    "NOT_RUN": "Not run",
}


_TASK_STATUS_EMOJI = {
    TaskStatus.RUNNING: "🚀",
    TaskStatus.SUCCEEDED: "✅",
    TaskStatus.FAILED: "🔴",
    TaskStatus.TIMED_OUT: "⏰",
    TaskStatus.ABORTED: "⛔",
    TaskStatus.SKIPPED: "↪️",
    TaskStatus.NOT_RUN: "—",
}


def _format_duration_sec(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    secs = max(0, int(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _format_utc(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_archive_cell(archive: object, page_status_arn: str) -> str:
    """Render the 'Latest output' cell.

    For ArchivePageRef → markdown link to the dashboard page slug.
    For ArtifactReason → italic verbatim reason string.
    For None → registry-drift sentinel (only renders if a state ships
    without a registry entry; the planned CI test in Phase 2 catches
    this at PR time but the page renders defensively).
    """
    if isinstance(archive, ArchivePageRef):
        # Relative path lets Streamlit's multipage navigation resolve
        # the link without baking the host into every cell.
        return f"[{archive.artifact_label}]({archive.page})"
    if isinstance(archive, ArtifactReason):
        return f"_{archive.reason}_"
    return "_⚠️ Registry drift — state not in STATE_TO_ARCHIVE_PAGE; file a fix._"


def _render_banner(result: LoadResult) -> None:
    """Render the per-section status banner.

    Green = live; blue = live-but-role-fallback (named filter); yellow =
    cache fallback (named age); red = no cache available (named error).
    NO_EXECUTIONS is treated as info, not error — the SF is healthy, it
    just hasn't run yet.
    """
    if result.outcome == LoadOutcome.LIVE:
        st.success(
            f"Live ✓ — fresh poll from states:DescribeExecution "
            f"({datetime.now(timezone.utc).strftime('%H:%M:%S UTC')})"
        )
        return

    if result.outcome == LoadOutcome.LIVE_ROLE_FALLBACK:
        st.info(
            f"ℹ️ {result.error_message or 'Role filter matched no executions; showing most recent.'}"
        )
        return

    if result.outcome == LoadOutcome.NO_EXECUTIONS:
        st.info(
            f"No executions yet for this state machine. "
            f"({result.error_message or 'awaiting first run'})"
        )
        return

    if result.outcome == LoadOutcome.CACHE:
        age_str = (
            f"{int(result.cache_age_seconds // 60)} min ago"
            if result.cache_age_seconds and result.cache_age_seconds >= 60
            else f"{int(result.cache_age_seconds or 0)}s ago"
        )
        st.warning(
            f"⚠️ Showing last-good cache (live SFN call failed). "
            f"Cache age: {age_str}. Live error: {result.error_message}"
        )
        return

    # outcome == ERROR
    st.error(
        f"🔴 SFN read failed AND no cache available. {result.error_message}"
    )


def _render_run_header(run: Optional[PipelineRun], arn: str) -> None:
    """Top-of-section metadata strip."""
    label = PIPELINE_LABELS.get(arn.rsplit(":", 1)[-1], arn.rsplit(":", 1)[-1])
    if run is None:
        st.subheader(f"{label}")
        return

    # Headline the artifact-completion VERDICT, not the raw SF terminal
    # status — a run that produced every artifact but tripped a Catch /
    # DataLimitExceeded / terminal-notify state still reports FAILED
    # (config#727 / #856). The raw SF status + failing-state detail are kept
    # below for the operator (this is the gated console, not the public site).
    cv = derive_cycle_verdict(run)
    v_emoji = _VERDICT_EMOJI.get(cv.verdict, "❓")
    v_label = _VERDICT_LABEL.get(cv.verdict, cv.verdict.title())
    artifact_frag = (
        f" · {cv.artifacts_produced}/{cv.artifacts_total} artifacts"
        if cv.artifacts_total
        else ""
    )
    # Role badge surfaces whether this execution is the canonical
    # cadence run (weekly / daily / eod) or a smoke / recovery /
    # operator-replay overlay. Pre-Option-D executions render as
    # "role: unknown" until the new cron rule's first cadence firing.
    st.subheader(
        f"{v_emoji} {run.pretty_label} — {v_label}{artifact_frag}  "
        f"{_role_badge(run.pipeline_role)}"
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Start (UTC)", _format_utc(run.start_utc))
    col2.metric("End (UTC)", _format_utc(run.end_utc))
    col3.metric("Duration", _format_duration_sec(run.duration_sec))

    # Transparency: when the cycle produced its artifacts but the SF still
    # exited non-OK, surface the divergence as an info note rather than the
    # alarming red error block — the cycle succeeded; the plumbing tripped.
    if cv.diverges_from_dag and run.status != RunStatus.SUCCEEDED:
        st.info(
            f"Step Function exited **{_RUN_STATUS_EMOJI.get(run.status, '❓')} "
            f"{run.status.value}** at a non-artifact step "
            f"(`{run.failing_state or 'unknown'}`) — all tracked artifacts "
            "were produced, so the cycle is treated as complete."
        )
        if run.failure_cause:
            with st.expander("SF failure cause (non-artifact step)"):
                st.code(run.failure_cause, language="text")
    elif run.status == RunStatus.FAILED and (run.failing_state or run.failure_cause):
        st.error(
            f"**Failed at state**: `{run.failing_state or 'unknown'}`  \n"
            f"**Cause**: {run.failure_cause or '(empty)'}"
        )

    if run.execution_name:
        st.caption(f"Execution: `{run.execution_name}`")


def _render_task_table(run: PipelineRun, status_arn: str) -> None:
    if not run.tasks:
        st.info("No substantive Task states yet for this execution.")
        return

    rows = []
    for task in run.tasks:
        rows.append(
            {
                "State": task.state_name,
                "Status": f"{_TASK_STATUS_EMOJI.get(task.status, '❓')} {task.status.value}",
                "Start (UTC)": _format_utc(task.start_utc),
                "Duration": _format_duration_sec(task.duration_sec),
                "Latest output": _format_archive_cell(task.archive, status_arn),
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Latest output": st.column_config.LinkColumn(
                "Latest output",
                help="Deep-link to the artifact-archive page for this state's output. "
                "Italic text = substrate-only state, no per-run rendered artifact.",
            ),
        },
    )

    # Per-state failure cause expansion when present
    failed_tasks = [t for t in run.tasks if t.status == TaskStatus.FAILED and t.failure_cause]
    if failed_tasks:
        with st.expander(f"Failure cause details ({len(failed_tasks)} failed states)"):
            for task in failed_tasks:
                st.markdown(f"**{task.state_name}**")
                st.code(task.failure_cause, language="text")


def _render_recent_executions_disclosure(arn: str, canonical_role: Optional[str]) -> None:
    """Expander listing last 10 executions across ALL roles so the
    operator can see what's been running (smoke / recovery / etc) and
    click into any specific execution. Backed by
    ``list_recent_pipeline_runs_for_arn`` (one DescribeExecution per row
    to extract pipeline_role — bounded at 10 calls per render)."""
    sm_name = arn.rsplit(":", 1)[-1]
    session_key = f"pinned_execution_{sm_name}"

    with st.expander(f"📜 View other recent executions of {sm_name}", expanded=False):
        try:
            summaries = list_recent_pipeline_runs_for_arn(arn, limit=10)
        except Exception as exc:  # noqa: BLE001 — surface inline
            st.warning(
                f"Could not list recent executions: {type(exc).__name__}: {exc}"
            )
            return

        if not summaries:
            st.info("No executions to list.")
            return

        # Render a clickable row per execution. Click pins that execution
        # for the section above (st.rerun rebuilds with the pinned arn).
        for s in summaries:
            cols = st.columns([3, 1, 1, 2, 1])
            with cols[0]:
                st.code(s.name, language="text")
            with cols[1]:
                st.markdown(f"{_RUN_STATUS_EMOJI.get(s.status, '❓')} {s.status.value}")
            with cols[2]:
                st.markdown(_role_badge(s.pipeline_role))
            with cols[3]:
                st.caption(
                    f"{_format_utc(s.start_utc)} · {_format_duration_sec(s.duration_sec)}"
                )
            with cols[4]:
                # The button key must be unique per SF + per execution
                # to survive Streamlit's widget-key-uniqueness check.
                button_key = f"pin_{sm_name}_{s.execution_arn}"
                if st.button("Inspect ▸", key=button_key):
                    st.session_state[session_key] = s.execution_arn
                    st.rerun()

        # Offer a "clear pin" affordance when an execution is pinned —
        # otherwise the operator's stuck on the chosen execution until
        # the cache TTL expires.
        if st.session_state.get(session_key):
            if st.button(
                "↺ Return to canonical cadence view",
                key=f"clear_pin_{sm_name}",
            ):
                st.session_state.pop(session_key, None)
                st.rerun()


def _resolve_run_name_to_arn(arn: str, run_name: Optional[str]) -> Optional[str]:
    """Resolve a ``?run=`` execution NAME to its execution ARN for this SF.

    The SF failure/complete notifications (nousergon-data) deep-link to
    ``…/pipeline-status?run=<execution-name>`` where ``<execution-name>`` is
    the Step Function ``$$.Execution.Name`` — the trailing segment of the
    execution ARN. The picker keys on ARN, so we scan the recent-execution
    window for this state machine and return the ARN whose execution NAME
    (or, belt-and-suspenders, the trailing name segment of its ARN) matches
    ``run_name``.

    Returns ``None`` when ``run_name`` is empty or no recent execution of
    this SF matches — the caller then falls back to the canonical
    most-recent auto-pick (never errors, never blanks). Listing errors are
    swallowed to ``None`` for the same reason: a stale/bad deep-link must
    never break the page render.
    """
    if not run_name:
        return None
    try:
        # Cross-role listing (role_filter=None) so a ?run= link to a smoke /
        # recovery / replay execution still resolves — the operator (or the
        # notification) asked for that specific run by name.
        summaries = list_recent_pipeline_runs_for_arn(arn, limit=10)
    except Exception:  # noqa: BLE001 — a bad deep-link must never break render
        return None
    for s in summaries:
        if s.name == run_name or s.execution_arn.rsplit(":", 1)[-1] == run_name:
            return s.execution_arn
    return None


def _render_section(arn: str, run_param: Optional[str] = None) -> None:
    canonical_role = _canonical_role_for(arn)
    sm_name = arn.rsplit(":", 1)[-1]
    session_key = f"pinned_execution_{sm_name}"
    pinned_arn = st.session_state.get(session_key)

    # ?run= deep-link: if a run name is supplied and matches an execution of
    # THIS state machine, select it. An explicit in-session pin (Inspect ▸
    # button) still wins. A non-matching / absent ?run= resolves to None and
    # falls through to the canonical most-recent auto-pick below — behavior
    # identical to before this param existed.
    run_param_arn = (
        _resolve_run_name_to_arn(arn, run_param) if not pinned_arn else None
    )

    if pinned_arn:
        # Operator pinned a specific execution via the disclosure.
        result = read_pipeline_state_with_fallback(arn, execution_arn=pinned_arn)
    elif run_param_arn:
        # ?run= matched a specific execution of this SF.
        result = read_pipeline_state_with_fallback(arn, execution_arn=run_param_arn)
    elif canonical_role:
        # Default: filter to canonical cadence role for this SF.
        result = read_pipeline_state_with_fallback(
            arn, role_filter={canonical_role}
        )
    else:
        # No canonical role registered (future SF added without an entry
        # in _CANONICAL_ROLE_BY_SF) — fall back to most-recent overall.
        result = read_pipeline_state_with_fallback(arn)

    _render_run_header(result.run, arn)
    _render_banner(result)

    if result.run is not None:
        _render_task_table(result.run, arn)

    _render_recent_executions_disclosure(arn, canonical_role)


# ── Page ──────────────────────────────────────────────────────────────────


st.title("🚦 Pipeline Status")
st.caption(
    "Per-Step-Function execution state from `states:DescribeExecution` + "
    "`states:GetExecutionHistory`. Per ROADMAP L3050 (pipeline-reporting-revamp). "
    "Cached 60s; refresh forces a live re-read."
)

# Refresh button — bypasses st.cache_data and writes the S3 last-good cache.
if st.button("🔄 Refresh now", help="Forces a live poll + writes the last-good S3 cache"):
    with st.spinner("Polling SFN…"):
        arns_with_filters: list[tuple[str, Optional[set[str]]]] = []
        for arn in _ALL_ARNS:
            canonical_role = _canonical_role_for(arn)
            arns_with_filters.append(
                (arn, {canonical_role} if canonical_role else None)
            )
        refresh_and_write_cache(arns_with_filters)
    st.rerun()

# Honor the ?run= deep-link from the SF failure/complete notifications
# (nousergon-data): …/pipeline-status?run=<execution-name> where the value is
# the Step Function $$.Execution.Name. Each section resolves it against its own
# recent-execution window; the one that owns that execution renders it, the
# others fall back to their canonical most-recent auto-pick. Absent/unknown
# ?run= leaves every section on its auto-pick (behavior unchanged).
_run_param = st.query_params.get("run")

for arn in _ALL_ARNS:
    st.divider()
    _render_section(arn, run_param=_run_param)

st.divider()
st.caption(
    "Substrate: `nousergon_lib.pipeline_status` (v0.28.1) + "
    "`alpha-engine-dashboard-sfn-read` IAM policy on `alpha-engine-executor-role`. "
    "Last-good cache: `s3://alpha-engine-research/dashboard/pipeline_status_cache.json`."
)
