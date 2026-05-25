"""
pipeline_status_loader.py — Page 25 loader.

Wraps ``alpha_engine_lib.pipeline_status.read_pipeline_state`` with:

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

import streamlit as st

from alpha_engine_lib.pipeline_status import (
    PipelineExecutionSummary,
    PipelineRun,
    SFNAccessDenied,
    SFNNoExecutions,
    SFNThrottled,
    list_recent_pipeline_runs,
    read_pipeline_state,
)
from alpha_engine_lib.pipeline_status.read import PipelineStatusError

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


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
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


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
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
