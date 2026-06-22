"""System Pulse loaders — curated, process-only datapoints for the public
live dashboard (ROADMAP L4570e, plan §9b.2).

Every loader returns a SMALL curated dict (the public-safe subset) or None
when its substrate is unavailable. The curation is the point: pipeline runs
are reduced to step names + statuses + timing (no failure causes, execution
names, or archive refs — that detail stays on the gated console per the
public-presence disclosure line); artifact freshness is reduced to counts
(no artifact IDs or S3 keys); cost is reduced to one total.

Failure posture: consumer-side graceful degrade is deliberate on this
read-only public surface — each miss is logged at WARN and the page renders
an "unavailable" caption. The producers' own monitoring (freshness monitor,
SF alarms, flow-doctor) is the fail-loud surface for the data itself.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    _research_bucket,
    download_s3_json,
    get_s3_client,
)

logger = logging.getLogger(__name__)

# Mirrors views/25_Pipeline_Status.py (the console page over the same
# substrate). The ARNs are derivable from the public CFN templates in
# alpha-engine-data; nothing secret is encoded here.
_REGION = "us-east-1"
_ACCOUNT_ID = "711398986525"

SATURDAY_ARN = (
    f"arn:aws:states:{_REGION}:{_ACCOUNT_ID}:stateMachine:alpha-engine-saturday-pipeline"
)
WEEKDAY_ARN = (
    f"arn:aws:states:{_REGION}:{_ACCOUNT_ID}:stateMachine:alpha-engine-weekday-pipeline"
)

_FRESHNESS_HEARTBEAT_KEY = "_freshness_monitor/heartbeat.json"
_COST_PREFIX = "decision_artifacts/_cost/"


# ── Pure curation helpers (unit-tested; no I/O) ──────────────────────────


# A TaskRow whose ``archive`` is tagged ``archive_page_ref`` (ArchivePageRef)
# is a substantive state whose SUCCESS produces an operator-readable
# artifact; an ``artifact_reason`` tag (ArtifactReason) marks a substrate /
# notification / operational step that produces no per-run artifact. We
# judge a CYCLE by whether it produced its artifacts (config#727 artifact-
# freshness principle + config#856 pull-for-state), NOT by the Step
# Function's terminal DAG status: a run that wrote every artifact but then
# tripped a Catch / States.DataLimitExceeded / a terminal-notify state
# (HandleFailure, NotifyComplete) still reports RunStatus.FAILED — which is
# a plumbing fail at a non-artifact step, not a failed research cycle.
_ARTIFACT_KIND = "archive_page_ref"


def _status_str(status: Any) -> str:
    return str(getattr(status, "value", status) or "").upper()


def derive_cycle_verdict(run: Any) -> dict:
    """Artifact-completion verdict for a pipeline run.

    Returns ``{"verdict", "artifacts_produced", "artifacts_total"}``. The
    verdict is one of:

    - ``RUNNING`` / ``NOT_RUN`` — passed through from the DAG status.
    - ``COMPLETE`` — every artifact-bearing state SUCCEEDED (regardless of
      the DAG terminal status — this is the fix for the false-FAIL headline).
    - ``PARTIAL`` — some but not all artifact-bearing states SUCCEEDED.
    - ``FAILED`` — no artifact-bearing state SUCCEEDED.

    When the run carries no artifact-bearing telemetry (``artifacts_total``
    == 0 — e.g. a run that died before any substantive state, or a curated
    fixture without ``archive`` tags) the verdict falls back to the raw DAG
    status so we never manufacture a green from absent evidence.
    """
    dag = _status_str(getattr(run, "status", None))
    produced = total = 0
    for t in getattr(run, "tasks", None) or []:
        if getattr(getattr(t, "archive", None), "kind", None) != _ARTIFACT_KIND:
            continue
        total += 1
        if _status_str(getattr(t, "status", None)) == "SUCCEEDED":
            produced += 1

    if dag == "RUNNING":
        verdict = "RUNNING"
    elif dag in ("NOT-RUN", "NOT_RUN"):
        verdict = "NOT_RUN"
    elif total == 0:
        verdict = "COMPLETE" if dag == "SUCCEEDED" else "FAILED"
    elif produced == total:
        verdict = "COMPLETE"
    elif produced > 0:
        verdict = "PARTIAL"
    else:
        verdict = "FAILED"

    return {
        "verdict": verdict,
        "artifacts_produced": produced,
        "artifacts_total": total,
    }


def curate_pipeline_run(run: Any) -> dict:
    """Reduce a lib PipelineRun to the public-safe subset.

    Keeps: artifact-completion verdict + artifact counts, the raw DAG
    terminal status (shown as secondary context so a genuine SF failure is
    never hidden), per-task name / status / start / duration. Drops:
    failure_cause, execution names, archive refs — internal detail that
    belongs on the gated console only.
    """
    tasks = []
    for t in getattr(run, "tasks", None) or []:
        start = getattr(t, "start_utc", None)
        tasks.append(
            {
                "name": getattr(t, "state_name", "?"),
                "status": _status_str(getattr(t, "status", None)),
                "start_utc": start.isoformat() if start is not None else None,
                "duration_sec": getattr(t, "duration_sec", None),
            }
        )
    start = getattr(run, "start_utc", None)
    return {
        # ``status`` stays the raw SF terminal status (the page renders it
        # only as secondary context when it diverges from the verdict).
        "status": _status_str(getattr(run, "status", None)),
        "start_utc": start.isoformat() if start is not None else None,
        "tasks": tasks,
        **derive_cycle_verdict(run),
    }


def summarize_freshness(heartbeat: dict | None) -> dict | None:
    """Reduce the freshness-monitor heartbeat to public-safe counts."""
    if not heartbeat:
        return None
    counts = heartbeat.get("counts") or {}
    n_total = heartbeat.get("n_entries_checked") or sum(counts.values())
    if not n_total:
        return None
    return {
        "n_total": int(n_total),
        "within_sla": int(counts.get("fresh", 0)) + int(counts.get("grace_period", 0)),
        "stale": int(counts.get("stale", 0)),
        "missing": int(counts.get("missing", 0)),
        "probe_failed": int(counts.get("probe_failed", 0)),
        "last_run": heartbeat.get("last_run"),
    }


def summarize_activity(signals: dict | None) -> dict | None:
    """Reduce signals.json to research-cycle activity counts.

    Only counts derivable from the artifact itself — no static funnel
    claims (per the public-copy claims discipline).
    """
    if not signals:
        return None
    return {
        "date": signals.get("date"),
        "regime": signals.get("market_regime"),
        "tracked": len(signals.get("universe") or []),
        "population": len(signals.get("population") or []),
        "buy_candidates": len(signals.get("buy_candidates") or []),
    }


def summarize_cost(df: pd.DataFrame | None, capture_date: str) -> dict | None:
    """Reduce one weekly cost parquet to a single spend datapoint."""
    if df is None or df.empty or "cost_usd" not in df.columns:
        return None
    return {
        "capture_date": capture_date,
        "total_usd": float(pd.to_numeric(df["cost_usd"], errors="coerce").fillna(0).sum()),
        "n_calls": int(len(df)),
    }


# ── Cached I/O wrappers ──────────────────────────────────────────────────


@st.cache_data(ttl=300, show_spinner=False)
def load_pipeline_run(arn: str, role: str) -> dict | None:
    """Most recent cadence run for one Step Function, curated.

    role_filter keeps smoke / recovery / operator-replay executions from
    displacing the cadence run (mirrors the console's canonical-role read).
    """
    try:
        from nousergon_lib.pipeline_status import read_pipeline_state

        return curate_pipeline_run(read_pipeline_state(arn, role_filter={role}))
    except Exception as exc:  # noqa: BLE001 — consumer-side degrade; WARN is the recording surface (module docstring)
        logger.warning(
            "system-pulse: pipeline read failed for %s: %s", arn.rsplit(":", 1)[-1], exc
        )
        return None


@st.cache_data(ttl=300, show_spinner=False)
def load_freshness_summary() -> dict | None:
    """Artifact-freshness SLA counts from the monitor's heartbeat."""
    try:
        return summarize_freshness(
            download_s3_json(_research_bucket(), _FRESHNESS_HEARTBEAT_KEY)
        )
    except Exception as exc:  # noqa: BLE001 — consumer-side degrade; WARN is the recording surface (module docstring)
        logger.warning("system-pulse: freshness heartbeat read failed: %s", exc)
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def load_research_activity() -> dict | None:
    """Latest research-cycle activity counts from signals.json."""
    try:
        from loaders.s3_loader import load_latest_signals

        return summarize_activity(load_latest_signals())
    except Exception as exc:  # noqa: BLE001 — consumer-side degrade; WARN is the recording surface (module docstring)
        logger.warning("system-pulse: signals read failed: %s", exc)
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def load_latest_cost_summary() -> dict | None:
    """Latest weekly LLM-spend total from the cost parquet."""
    try:
        client = get_s3_client()
        resp = client.list_objects_v2(
            Bucket=_research_bucket(), Prefix=_COST_PREFIX, Delimiter="/"
        )
        dates = sorted(
            p["Prefix"].removeprefix(_COST_PREFIX).strip("/")
            for p in resp.get("CommonPrefixes", [])
        )
        if not dates:
            return None
        latest = dates[-1]
        obj = client.get_object(
            Bucket=_research_bucket(), Key=f"{_COST_PREFIX}{latest}/cost.parquet"
        )
        df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
        return summarize_cost(df, latest)
    except Exception as exc:  # noqa: BLE001 — consumer-side degrade; WARN is the recording surface (module docstring)
        logger.warning("system-pulse: cost parquet read failed: %s", exc)
        return None
