"""
pipeline_status_loader.py — Page 25 loader.

Wraps ``nousergon_lib.pipeline_status.read_pipeline_state`` with:

- Streamlit cache (60s TTL — short enough for page 25's "open daily and
  trust on transitions" operational pattern, long enough to not hammer
  the SF API on every Streamlit rerun).
- S3 last-good cache (``s3://alpha-engine-research/dashboard/pipeline_status_cache.json``)
  written after every successful poll, read as a fallback when the live
  SFN call throttles or 5xx's.
- Typed result shape distinguishing "live" / "cache-fallback" /
  "no-executions" so the page can render the right banner state.

Per ``feedback_no_silent_fails``, the loader NEVER swallows exceptions
silently. A red banner on the page surfaces every failure mode by name
(IAM denial / throttle / unknown — the lib's typed exceptions decide
which); the cache fallback is a SECONDARY graceful-degrade path that
preserves operator visibility into the most recent good state. Both
fail-loud (via banner + S3 error log) AND graceful-degrade (via cache)
coexist — they are NOT alternatives.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import boto3

from loaders.cache import cached

from nousergon_lib.pipeline_status import (
    read_reliability_window,
    PipelineExecutionSummary,
    PipelineRun,
    RunStatus,
    SFNAccessDenied,
    SFNNoExecutions,
    SFNThrottled,
    TaskStatus,
    list_recent_pipeline_runs,
    read_pipeline_state,
)
from nousergon_lib.pipeline_status.read import PipelineStatusError

from loaders.s3_loader import (
    _record_s3_error,
    _research_bucket,
    download_s3_json,
    get_s3_client,
)


logger = logging.getLogger(__name__)


_CACHE_S3_KEY = "dashboard/pipeline_status_cache.json"
_CACHE_TTL_SECONDS = 60


class LoadOutcome(str, Enum):
    """Provenance of the PipelineRun returned to the page."""

    LIVE = "live"  # fresh SFN poll succeeded
    LIVE_ROLE_FALLBACK = "live_role_fallback"  # role_filter found nothing; fell back to most-recent overall
    CACHE = "cache"  # SFN failed; rendering last-good from S3 cache
    NO_EXECUTIONS = "no_executions"  # SF exists but has no history
    ERROR = "error"  # SFN failed AND no cache available


# An ArchivePageRef-tagged Task (TaskRow.archive.kind == "archive_page_ref")
# is a substantive state whose SUCCESS produces an operator-readable
# artifact; ArtifactReason states are substrate / notification / operational
# only. A cycle is judged COMPLETE by whether it produced its artifacts —
# NOT the SF terminal RunStatus — because a run that wrote every artifact and
# then tripped a Catch / States.DataLimitExceeded / a terminal-notify state
# (HandleFailure, NotifyComplete) still reports RunStatus.FAILED. That's a
# plumbing fail at a non-artifact step, not a failed cycle (config#727 /
# config#856).
_ARTIFACT_KIND = "archive_page_ref"


@dataclass(frozen=True)
class CycleVerdict:
    """Artifact-completion verdict for a PipelineRun (see derive_cycle_verdict)."""

    verdict: str  # COMPLETE | PARTIAL | FAILED | RUNNING | NOT_RUN
    artifacts_produced: int
    artifacts_total: int

    @property
    def diverges_from_dag(self) -> bool:
        """True when the run produced its artifacts but the SF still exited non-OK."""
        return self.verdict == "COMPLETE"


def derive_cycle_verdict(run: PipelineRun) -> CycleVerdict:
    """Project a PipelineRun onto an artifact-completion verdict.

    ``COMPLETE`` (every artifact-bearing state SUCCEEDED, regardless of the
    DAG terminal status), ``PARTIAL`` (some), ``FAILED`` (none), or
    ``RUNNING`` / ``NOT_RUN`` passed through. Falls back to the raw DAG
    status when the run carries no artifact-bearing telemetry, so we never
    manufacture a green from absent evidence.

    NOTE: mirrors ``live/loaders/system_pulse_loader.derive_cycle_verdict``
    (the public surface). Both should be lifted into
    ``nousergon_lib.pipeline_status`` on the next lib bump — second
    adoption is the consolidation signal; bundle with the registry work on
    config#1102.
    """
    dag = run.status
    produced = total = 0
    for t in run.tasks:
        if getattr(t.archive, "kind", None) != _ARTIFACT_KIND:
            continue
        total += 1
        if t.status == TaskStatus.SUCCEEDED:
            produced += 1

    if dag == RunStatus.RUNNING:
        verdict = "RUNNING"
    elif dag == RunStatus.NOT_RUN:
        verdict = "NOT_RUN"
    elif total == 0:
        verdict = "COMPLETE" if dag == RunStatus.SUCCEEDED else "FAILED"
    elif produced == total:
        verdict = "COMPLETE"
    elif produced > 0:
        verdict = "PARTIAL"
    else:
        verdict = "FAILED"
    return CycleVerdict(verdict, produced, total)


# ── Gate verdict (alpha-engine-config-I7313) ────────────────────────────────
#
# COMPLETE (derive_cycle_verdict, above) and VERIFIED (below) are two
# separate axes. A run can produce every artifact and still have spent
# without its correctness gates ever measuring — sf-pipeline-policy.md
# §2.3a rule 3: any surface presenting a run's numbers must carry the
# verdict's state. Every degraded-gate SNS alert deep-links here
# (nousergon-data step_function.json's NotifyComplete* constants), which
# makes this the one place the omission cost the most: the alert correctly
# said the gate degraded, and the page it pointed at said nothing.
#
# Data source: the five SF-controlled degraded-flag families threaded onto
# the execution's own $ scope by exactly one Pass state each
# (gate_degraded / health_check_degraded / report_card_degraded /
# parity_degraded / research_predictor_degraded — verified live 2026-08-14
# against nousergon-data/infrastructure/step_function.json), landing on
# DescribeExecution's ``output`` for a terminal execution. Consumed here
# by a SEPARATE DescribeExecution read rather than crucible-evaluator's
# normalized ``grading/pipeline_gates.py::read_gate_state`` block:
# crucible-evaluator is not an installable dependency of crucible-dashboard
# (no setup.py/pyproject.toml, not in requirements.in), and as of this PR
# its fix for the alpha-engine-config-I7312 verdict defect
# (crucible-evaluator-PR205) is open but unmerged — consuming it now would
# import that bug. Mirrored instead: nousergon-data's own
# ``CheckGateDegradedNotify`` Choice semantics, ``And(IsPresent, Boolean-
# Equals)`` per flag — absence is NEVER read as False (config#2275
# invariant) — and crucible-evaluator's VERIFIED / NOT VERIFIED calibration
# (grading/pipeline_gates.py::_statement): an absent flag is an absence of
# evidence, never a clean pass; a fired flag is a detected condition and
# renders distinctly from both.
#
# Verified live 2026-08-14: every SUCCEEDED execution of
# ne-weekly-freshness-pipeline sampled (7 executions, 2026-07-31 through
# 2026-08-13) carries NONE of the five flags — they are omitted entirely on
# every production run to date, never present-and-false. Under this module
# every one of those runs renders NOT_VERIFIED, replacing the prior silent
# "renders as COMPLETE, says nothing about gates" behavior — this is the
# defect the issue describes, confirmed on live data, not a hypothetical.

_DEGRADED_FAMILY_LABELS: dict[str, str] = {
    "gate_degraded": "pre-spend gates (LibPinDriftCheck/PipelineContractCheck/AcquireMutex)",
    "health_check_degraded": "tail health checks (SaturdayHealthCheck/WeeklySubstrateHealthCheck)",
    "report_card_degraded": "report card advisory grading (ReportCard Lambda)",
    "parity_degraded": "parity verdict (pit_parity/replay)",
    "research_predictor_degraded": "an internal ResearchPredictorParallel fail-open",
}


class GateVerdict(str, Enum):
    """The run's correctness-gate axis — orthogonal to CycleVerdict's
    artifact-completion axis. A run can be COMPLETE and still be
    NOT_VERIFIED or DEGRADED here."""

    VERIFIED = "verified"  # all 5 families reported, none fired true
    DEGRADED = "degraded"  # at least one family reported true — a detected condition
    NOT_VERIFIED = "not_verified"  # at least one family absent — an absence of evidence


@dataclass(frozen=True)
class GateVerdictResult:
    """Outcome of one gate-verdict read. Never constructed as VERIFIED from
    absent, unparseable, or unreachable data — see read_gate_verdict_with_fallback."""

    verdict: GateVerdict
    degraded_families: tuple[str, ...] = ()
    unreported_families: tuple[str, ...] = ()
    error_message: Optional[str] = None

    @property
    def summary(self) -> str:
        if self.verdict == GateVerdict.VERIFIED:
            return "All 5 SF-controlled degraded-flag families reported, none fired."
        if self.verdict == GateVerdict.DEGRADED:
            named = "; ".join(
                _DEGRADED_FAMILY_LABELS.get(f, f) for f in self.degraded_families
            )
            return (
                f"Fail-open degradation recorded on this run: {named}. "
                "Every number was still computed; treat them as UNATTESTED, not as wrong."
            )
        if self.error_message:
            return self.error_message
        named = "; ".join(
            _DEGRADED_FAMILY_LABELS.get(f, f) for f in self.unreported_families
        )
        return (
            f"The Step Function reported no value for: {named} — "
            "unreported is not false."
        )


def _gate_verdict_from_execution_output(raw_output: Optional[str]) -> GateVerdictResult:
    """Pure projection of a DescribeExecution ``output`` JSON string onto a
    :class:`GateVerdictResult`. Mirrors ``CheckGateDegradedNotify``'s
    And(IsPresent, BooleanEquals) semantics per flag; a fired family always
    wins over an unrelated absence (mirrors that Choice's most-specific-
    first ordering, which routes on ANY true flag regardless of what else
    is unreported)."""
    if not raw_output:
        return GateVerdictResult(
            GateVerdict.NOT_VERIFIED,
            unreported_families=tuple(_DEGRADED_FAMILY_LABELS),
            error_message="No execution output available (run has not reached a terminal state with output).",
        )
    try:
        parsed = json.loads(raw_output)
    except (ValueError, TypeError) as exc:
        return GateVerdictResult(
            GateVerdict.NOT_VERIFIED,
            unreported_families=tuple(_DEGRADED_FAMILY_LABELS),
            error_message=f"Could not parse execution output as JSON: {type(exc).__name__}.",
        )
    if not isinstance(parsed, dict):
        return GateVerdictResult(
            GateVerdict.NOT_VERIFIED,
            unreported_families=tuple(_DEGRADED_FAMILY_LABELS),
            error_message="Execution output was not a JSON object.",
        )

    degraded = tuple(f for f in _DEGRADED_FAMILY_LABELS if parsed.get(f) is True)
    unreported = tuple(
        f for f in _DEGRADED_FAMILY_LABELS if not isinstance(parsed.get(f), bool)
    )

    if degraded:
        return GateVerdictResult(
            GateVerdict.DEGRADED, degraded_families=degraded, unreported_families=unreported
        )
    if unreported:
        return GateVerdictResult(GateVerdict.NOT_VERIFIED, unreported_families=unreported)
    return GateVerdictResult(GateVerdict.VERIFIED)


def _sfn_client_for(state_machine_arn: str):
    """Return a boto3 Step Functions client for the ARN's region.

    Uses the EC2 IAM role automatically (mirrors loaders.s3_loader.get_s3_client).
    """
    region = state_machine_arn.split(":")[3] if state_machine_arn.startswith("arn:") else None
    return boto3.client("stepfunctions", region_name=region)


@cached(ttl=_CACHE_TTL_SECONDS)
def _cached_gate_verdict_output(state_machine_arn: str, execution_arn: str) -> Optional[str]:
    """Streamlit-cached raw ``output`` JSON string for one execution.

    A separate DescribeExecution call from the one ``read_pipeline_state``
    already makes internally — nousergon_lib's PipelineRun does not expose
    ``output`` (alpha-engine-config-I7313; nousergon-lib's read.py never
    reads ``describe_resp.get("output")``). Raises on any boto3 failure;
    the caller (read_gate_verdict_with_fallback) is the one that decides
    what an unreadable execution renders as — never VERIFIED.
    """
    client = _sfn_client_for(state_machine_arn)
    resp = client.describe_execution(executionArn=execution_arn)
    return resp.get("output")


_GATE_VERDICT_TERMINAL_STATUSES = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.TIMED_OUT,
    RunStatus.ABORTED,
)


def read_gate_verdict_with_fallback(
    state_machine_arn: str,
    execution_arn: Optional[str],
    run_status: RunStatus,
) -> GateVerdictResult:
    """Public loader for the page-25 gate-verdict badge.

    Per alpha-engine-config-I7313 deliverable 3: absence of the verdict
    data renders as unknown, never as verified. A missing execution, a
    non-terminal run, or ANY read/parse failure all resolve to
    NOT_VERIFIED with a named reason — this function has no path that
    returns VERIFIED except a positively-read, positively-parsed output
    carrying all 5 families with none true.
    """
    if execution_arn is None:
        return GateVerdictResult(
            GateVerdict.NOT_VERIFIED,
            unreported_families=tuple(_DEGRADED_FAMILY_LABELS),
            error_message="No execution to read a gate verdict from.",
        )
    if run_status not in _GATE_VERDICT_TERMINAL_STATUSES:
        return GateVerdictResult(
            GateVerdict.NOT_VERIFIED,
            unreported_families=tuple(_DEGRADED_FAMILY_LABELS),
            error_message="Execution has not reached a terminal state yet.",
        )
    try:
        raw_output = _cached_gate_verdict_output(state_machine_arn, execution_arn)
    except Exception as exc:  # noqa: BLE001 — never silently render VERIFIED on a read failure
        logger.warning("gate-verdict read failed for %s: %s", execution_arn, exc)
        return GateVerdictResult(
            GateVerdict.NOT_VERIFIED,
            unreported_families=tuple(_DEGRADED_FAMILY_LABELS),
            error_message=f"Could not read execution output: {type(exc).__name__}: {exc}",
        )
    return _gate_verdict_from_execution_output(raw_output)


@dataclass(frozen=True)
class LoadResult:
    """Outcome of one ``read_pipeline_state_cached`` call.

    The page consumes ``outcome`` to render the banner; ``run`` is the
    payload to render the table from (None iff outcome == ERROR or
    NO_EXECUTIONS); ``error_message`` carries the human-readable cause
    for the banner (always populated when outcome != LIVE).
    """

    arn: str
    outcome: LoadOutcome
    run: Optional[PipelineRun]
    error_message: Optional[str]
    cache_age_seconds: Optional[float] = None


# ── Live + cache I/O ──────────────────────────────────────────────────────


def _write_last_good_cache(runs_by_arn: dict[str, PipelineRun]) -> None:
    """Serialize the latest good PipelineRuns to the S3 cache.

    Writes the full set (all 3 SFs) in one round-trip so the consumer
    reads a coherent snapshot. Best-effort — failure to write does not
    propagate; logged + recorded in the dashboard's S3 error tracker.

    Schema (jsonable):
      {
        "written_utc": "2026-05-24T15:42:31Z",
        "runs": {
          "<sf-arn>": <PipelineRun.model_dump JSON-safe>
        }
      }
    """
    payload = {
        "written_utc": datetime.now(timezone.utc).isoformat(),
        "runs": {arn: run.model_dump(mode="json") for arn, run in runs_by_arn.items()},
    }
    try:
        client = get_s3_client()
        client.put_object(
            Bucket=_research_bucket(),
            Key=_CACHE_S3_KEY,
            Body=json.dumps(payload, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget cache write
        logger.warning("pipeline_status_cache write failed: %s", exc)
        _record_s3_error(
            _research_bucket(),
            _CACHE_S3_KEY,
            type(exc).__name__,
            f"cache write failed: {exc}",
        )


def _read_last_good_cache_for_arn(arn: str) -> tuple[Optional[PipelineRun], Optional[float]]:
    """Read the cache and return (run-for-arn, cache-age-seconds) or (None, None).

    Cache-age is reported so the page banner can render "Last live: N min
    ago" — operator's primary signal that the page is showing fallback data.
    """
    raw = download_s3_json(_research_bucket(), _CACHE_S3_KEY)
    if not raw or not isinstance(raw, dict):
        return None, None
    runs = raw.get("runs") or {}
    arn_payload = runs.get(arn)
    if not arn_payload:
        return None, None
    try:
        run = PipelineRun.model_validate(arn_payload)
    except Exception as exc:  # noqa: BLE001 — degenerate cache
        logger.warning("pipeline_status_cache parse failed for %s: %s", arn, exc)
        return None, None

    cache_age: Optional[float] = None
    written = raw.get("written_utc")
    if written:
        try:
            written_dt = datetime.fromisoformat(written.replace("Z", "+00:00"))
            cache_age = (datetime.now(timezone.utc) - written_dt).total_seconds()
        except (ValueError, TypeError):
            pass
    return run, cache_age


# ── Public API (Streamlit-cached) ─────────────────────────────────────────


@cached(ttl=_CACHE_TTL_SECONDS)
def _cached_live_read(
    arn: str,
    role_filter_tuple: Optional[tuple[str, ...]] = None,
    execution_arn: Optional[str] = None,
) -> dict:
    """Streamlit-cached wrapper around live ``read_pipeline_state``.

    Returns a JSON-able dict so st.cache_data can hash it (PipelineRun
    instances are Pydantic but cache_data is happier with primitives).
    Caller re-validates back to PipelineRun.

    ``role_filter_tuple`` (not a set) because st.cache_data hashes the
    args; sets are unhashable so the public API takes a set and tuple-izes
    here.

    Raises:
      The typed lib exceptions (SFNAccessDenied / SFNThrottled /
      SFNNoExecutions / PipelineStatusError) propagate; the outer
      ``read_pipeline_state_with_fallback`` catches and routes.
    """
    role_filter = set(role_filter_tuple) if role_filter_tuple else None
    run = read_pipeline_state(
        arn, role_filter=role_filter, execution_arn=execution_arn
    )
    return run.model_dump(mode="json")


@cached(ttl=_CACHE_TTL_SECONDS)
def _cached_list_recent(
    arn: str, limit: int = 10, role_filter_tuple: Optional[tuple[str, ...]] = None
) -> list[dict]:
    """Streamlit-cached wrapper around ``list_recent_pipeline_runs``.

    Returns dicts (model_dump'd) for the same cache_data-friendliness
    reason as ``_cached_live_read``. Page-25 re-validates back to
    ``PipelineExecutionSummary`` on read.
    """
    role_filter = set(role_filter_tuple) if role_filter_tuple else None
    summaries = list_recent_pipeline_runs(
        arn, limit=limit, role_filter=role_filter
    )
    return [s.model_dump(mode="json") for s in summaries]


def list_recent_pipeline_runs_for_arn(
    arn: str, *, limit: int = 10, role_filter: Optional[set[str]] = None
) -> list[PipelineExecutionSummary]:
    """Page-25-facing wrapper that re-validates the cached dicts back
    into :class:`PipelineExecutionSummary` instances. Errors propagate
    to the caller (the disclosure expander renders the error inline)."""
    role_filter_tuple = (
        tuple(sorted(role_filter)) if role_filter is not None else None
    )
    raw = _cached_list_recent(arn, limit, role_filter_tuple)
    return [PipelineExecutionSummary.model_validate(d) for d in raw]


def read_pipeline_state_with_fallback(
    arn: str,
    *,
    role_filter: Optional[set[str]] = None,
    execution_arn: Optional[str] = None,
) -> LoadResult:
    """Public loader for page 25.

    Try live ``read_pipeline_state`` (cached 60s); on any error EXCEPT
    SFNNoExecutions, fall back to the S3 last-good cache. If the cache
    is also empty, return outcome=ERROR with a human-readable error
    message. SFNNoExecutions is its own terminal state — the page
    renders "no executions yet" cleanly without a red banner.

    Option-D execution-picker (2026-05-25):
    - ``role_filter`` filters to executions whose ``input.pipeline_role``
      ∈ ``role_filter`` (e.g. ``{"weekly"}`` for Saturday cadence). If
      no execution within the lib's search window matches, the loader
      AUTOMATICALLY FALLS BACK to most-recent overall with
      ``outcome=LIVE_ROLE_FALLBACK`` and an explanation message —
      the cutover window (pre-data-PR-deploy) and any future smoke-only
      windows BOTH render gracefully rather than going empty.
    - ``execution_arn`` requests a specific execution (dropdown click
      path). ``role_filter`` is ignored when ``execution_arn`` is set.

    Per ``feedback_no_silent_fails`` — every error path returns a typed
    outcome + specific error_message; the page renders both the banner
    AND the cache fallback (when present) so the operator sees both
    "we couldn't reach SFN, but here's the last-good state."
    """
    role_filter_tuple = (
        tuple(sorted(role_filter)) if role_filter is not None else None
    )
    try:
        live_dict = _cached_live_read(arn, role_filter_tuple, execution_arn)
        run = PipelineRun.model_validate(live_dict)
        return LoadResult(arn=arn, outcome=LoadOutcome.LIVE, run=run, error_message=None)
    except SFNNoExecutions as exc:
        # If a role_filter caused the empty result, fall back to
        # most-recent overall so the operator sees something. The page's
        # role-fallback banner names the filter that didn't match.
        if role_filter and execution_arn is None:
            try:
                fallback_dict = _cached_live_read(arn, None, None)
                fallback_run = PipelineRun.model_validate(fallback_dict)
                return LoadResult(
                    arn=arn,
                    outcome=LoadOutcome.LIVE_ROLE_FALLBACK,
                    run=fallback_run,
                    error_message=(
                        f"No execution with role in {sorted(role_filter)!r} "
                        "in the recent window — showing most recent overall."
                    ),
                )
            except Exception as inner_exc:  # noqa: BLE001 — fall through to NO_EXECUTIONS
                logger.warning(
                    "role-fallback failed for %s: %s", arn, inner_exc
                )
        return LoadResult(
            arn=arn,
            outcome=LoadOutcome.NO_EXECUTIONS,
            run=None,
            error_message=str(exc),
        )
    except SFNAccessDenied as exc:
        cached, age = _read_last_good_cache_for_arn(arn)
        return LoadResult(
            arn=arn,
            outcome=LoadOutcome.CACHE if cached else LoadOutcome.ERROR,
            run=cached,
            error_message=f"SFN access denied — {exc}",
            cache_age_seconds=age,
        )
    except SFNThrottled as exc:
        cached, age = _read_last_good_cache_for_arn(arn)
        return LoadResult(
            arn=arn,
            outcome=LoadOutcome.CACHE if cached else LoadOutcome.ERROR,
            run=cached,
            error_message=f"SFN throttled — {exc}",
            cache_age_seconds=age,
        )
    except PipelineStatusError as exc:
        cached, age = _read_last_good_cache_for_arn(arn)
        return LoadResult(
            arn=arn,
            outcome=LoadOutcome.CACHE if cached else LoadOutcome.ERROR,
            run=cached,
            error_message=f"SFN read failed — {exc}",
            cache_age_seconds=age,
        )
    except Exception as exc:  # noqa: BLE001 — unexpected boto3 path
        # Per feedback_no_silent_fails — even unanticipated errors get a
        # specific message; we don't return a generic "something went wrong".
        logger.exception("Unexpected error reading pipeline state for %s", arn)
        cached, age = _read_last_good_cache_for_arn(arn)
        return LoadResult(
            arn=arn,
            outcome=LoadOutcome.CACHE if cached else LoadOutcome.ERROR,
            run=cached,
            error_message=f"Unexpected: {type(exc).__name__}: {exc}",
            cache_age_seconds=age,
        )


def refresh_and_write_cache(
    arns_with_filters: list[tuple[str, Optional[set[str]]]]
) -> None:
    """Force a fresh poll of all ARNs (bypassing st.cache_data) and write
    the last-good cache. Called from the page's "Refresh" button.

    Each entry is ``(arn, role_filter)`` so the refresh uses the same
    filter the page will use on render — otherwise the cache would warm
    "most-recent overall" while the page asks for "most-recent weekly"
    and the live call would still pay the API cost.

    Skips writes for ARNs that fail to read live (we never overwrite a
    good cache with a bad poll).
    """
    # ``.clear`` is provided by st.cache_data only when Streamlit's
    # runtime context is active; in unit tests without that context the
    # decorator returns a plain function. Guard with getattr so the
    # refresh path stays callable from both production and test scopes.
    getattr(_cached_live_read, "clear", lambda: None)()
    getattr(_cached_list_recent, "clear", lambda: None)()
    good: dict[str, PipelineRun] = {}
    for arn, role_filter in arns_with_filters:
        role_tuple = (
            tuple(sorted(role_filter)) if role_filter is not None else None
        )
        try:
            live_dict = _cached_live_read(arn, role_tuple, None)
            good[arn] = PipelineRun.model_validate(live_dict)
        except Exception as exc:  # noqa: BLE001 — skip writes for failed ARNs
            logger.warning("refresh skipped for %s: %s", arn, exc)
            continue
    if good:
        _write_last_good_cache(good)


# ── Cycle-level reliability (alpha-engine-config-I6919) ───────────────────

# Declared stage spine per SF, used to rank how DEEP a cycle got before it
# failed. Order matters and membership matters: only substantive stages
# appear, so a poll or gate state entering is not mistaken for the run
# getting further. Names verified against the live definitions 2026-08-11.
RELIABILITY_STAGE_ORDER: dict[str, tuple[str, ...]] = {
    "ne-weekly-freshness-pipeline": (
        "MorningEnrich",
        "DataPhase1",
        "RAGIngestion",
        "Scanner",
        "SignalsEnvelope",
        "PredictorTraining",
        "DataPhase2",
        "Backtester",
        "ParityParallel",
        "PitParityCompare",
        "ModelZooSelect",
        "ModelZooTrainMap",
        # Split from a single `Evaluator` stage by alpha-engine-config-I3112 on
        # 2026-08-11. The old name survived here and ranked nothing, which is
        # the I6857 defect this module's test guards — a rename blinds every
        # reader matching on the old name, and the blindness reports as the
        # benign "that stage did not run".
        "EvaluatorDiagnostics",
        "EvaluatorOptimize",
        "ReportCard",
        "Director",
    ),
    "ne-preopen-trading-pipeline": (
        "StartExecutorEC2",
        "CodeFreshnessGate",
        "LaunchMorningEnrichSpot",
        "LaunchMorningArcticAppendSpot",
        # Scanner removed from this SF by nousergon-data-PR1464 (merged
        # 2026-08-20T18:25:51Z): per Brian's 2026-08-20 ruling the scanner
        # forms its cuts weekly rather than every weekday. The weekly SF's
        # Scanner stage (above, ne-weekly-freshness-pipeline) is unaffected.
        "PredictorInference",
        "CheckPredictorCoverage",
        "RunMorningPlanner",
        "RunDaemon",
    ),
    "ne-postclose-trading-pipeline": (
        "LaunchPostMarketDataSpot",
        "LaunchPostMarketArcticAppendSpot",
        "CaptureSnapshot",
        "EODReconcile",
        "StopTradingInstance",
    ),
}

# Reliability is O(scan_limit) SF API calls — one DescribeExecution and one
# GetExecutionHistory per execution scanned. A 60s TTL like the rest of this
# loader would put ~240 calls/minute on the page. 15 minutes is well inside
# the cadence it describes (a cycle is a day at fastest) while keeping a
# refresh cheap enough to be worth pressing.
_RELIABILITY_TTL_SECONDS = 900


@cached(ttl=_RELIABILITY_TTL_SECONDS)
def _cached_reliability(arn: str, max_cycles: int, scan_limit: int) -> list[dict]:
    """Streamlit-cached reliability read, flattened to primitives.

    Returns one dict per cycle rather than the ``ReliabilityWindow`` object:
    ``st.cache_data`` hashes and pickles its return value, and the dataclass
    graph round-trips badly. The page re-derives its aggregates from these
    rows, so nothing is lost and the cache stays inspectable.
    """
    window = read_reliability_window(
        arn,
        stage_order=RELIABILITY_STAGE_ORDER.get(arn.rsplit(":", 1)[-1], ()),
        max_cycles=max_cycles,
        scan_limit=scan_limit,
    )
    return [
        {
            "cycle_key": c.cycle_key,
            "attempts": c.attempt_count,
            "first_attempt_succeeded": c.first_attempt_succeeded,
            "attempts_to_success": c.attempts_to_success,
            "settled": c.settled,
            "recovered": c.recovered,
            "depth_index": c.depth_index,
            "depth_stage": c.depth_stage,
            "wall_clock_sec": c.wall_clock_sec,
            "new_causes": list(c.new_causes),
            "repeat_causes": list(c.repeat_causes),
            "unresolved_attempts": c.unresolved_attempts,
        }
        for c in window.cycles
    ]


@dataclass(frozen=True)
class ReliabilityResult:
    """Cycle rows plus the error, if any. Never both empty and silent."""

    cycles: list[dict]
    error: Optional[str] = None

    @property
    def clean_streak(self) -> int:
        streak = 0
        for row in reversed(self.cycles):
            if not row["settled"]:
                continue
            if row["first_attempt_succeeded"]:
                streak += 1
            else:
                break
        return streak

    @property
    def looping(self) -> Optional[bool]:
        """The most-recent settled cycle repeated an earlier cause.

        None when no cycle has settled — NOT False. The page renders the
        three states distinctly; collapsing "unknown" into "not looping" is
        the same error as rendering absence as green.
        """
        for row in reversed(self.cycles):
            if row["settled"] and row["attempts"]:
                return bool(row["repeat_causes"])
        return None

    @property
    def unresolved_attempts(self) -> int:
        return sum(r["unresolved_attempts"] for r in self.cycles)


def read_reliability_with_fallback(
    arn: str, *, max_cycles: int = 20, scan_limit: int = 120
) -> ReliabilityResult:
    """Public loader for the page-25 reliability strip.

    No S3 last-good fallback, deliberately unlike ``read_pipeline_state_
    with_fallback``: a stale reliability window would answer "are we making
    progress" with yesterday's verdict and no way for the reader to tell.
    An error returns empty rows AND the message, and the page says so.
    """
    try:
        return ReliabilityResult(cycles=_cached_reliability(arn, max_cycles, scan_limit))
    except PipelineStatusError as exc:
        logger.warning("reliability read failed for %s: %s", arn, exc)
        return ReliabilityResult(cycles=[], error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface, never swallow
        logger.warning("reliability read failed for %s: %s", arn, exc)
        return ReliabilityResult(cycles=[], error=f"{type(exc).__name__}: {exc}")
