"""
tests/test_pipeline_status_loader.py — Unit tests for
loaders/pipeline_status_loader.py.

Covers:
  - Live happy path → LoadOutcome.LIVE + cache write
  - SFNAccessDenied with cache present → LoadOutcome.CACHE + cache-age annotation
  - SFNAccessDenied with NO cache → LoadOutcome.ERROR
  - SFNThrottled with cache present → LoadOutcome.CACHE
  - SFNNoExecutions → LoadOutcome.NO_EXECUTIONS (NOT treated as error)
  - Unknown lib exception → LoadOutcome.CACHE (fallback) / ERROR (no cache)
  - refresh_and_write_cache skips writes when no ARNs read live successfully
  - Cache parse failure on degenerate S3 JSON → graceful (None, None) tuple

All boto3 + lib calls mocked; no live AWS / network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from alpha_engine_lib.pipeline_status import (
    PipelineRun,
    RunStatus,
    SFNAccessDenied,
    SFNNoExecutions,
    SFNThrottled,
    TaskStatus,
)
from alpha_engine_lib.pipeline_status.read import PipelineStatusError, TaskRow
from alpha_engine_lib.pipeline_status.registry import ArchivePageRef, ArtifactReason

from loaders.pipeline_status_loader import (
    LoadOutcome,
    LoadResult,
    derive_cycle_verdict,
    read_pipeline_state_with_fallback,
    refresh_and_write_cache,
    _read_last_good_cache_for_arn,
    _write_last_good_cache,
)


SAT_ARN = (
    "arn:aws:states:us-east-1:711398986525:stateMachine:alpha-engine-saturday-pipeline"
)


def _make_run(arn: str = SAT_ARN, status: RunStatus = RunStatus.SUCCEEDED) -> PipelineRun:
    return PipelineRun(
        state_machine_arn=arn,
        pretty_label="Saturday SF",
        execution_arn=f"{arn.replace('stateMachine', 'execution')}:test-run-1",
        execution_name="test-run-1",
        status=status,
        start_utc=datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 5, 24, 11, 30, tzinfo=timezone.utc),
        duration_sec=9000.0,
        tasks=[],
    )


# ── Live happy path ──────────────────────────────────────────────────────


def test_live_happy_path_returns_live_outcome():
    fake_run = _make_run()
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        return_value=fake_run.model_dump(mode="json"),
    ):
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert isinstance(result, LoadResult)
    assert result.outcome == LoadOutcome.LIVE
    assert result.run is not None
    assert result.run.pretty_label == "Saturday SF"
    assert result.error_message is None


# ── SFNAccessDenied + cache present ───────────────────────────────────────


def test_access_denied_with_cache_returns_cache_outcome_with_age():
    """Live SFN denied → load from cache, surface age annotation."""
    cached_run = _make_run()
    cache_age = 125.0  # 2 min ago
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=SFNAccessDenied("states:DescribeExecution denied"),
    ), patch(
        "loaders.pipeline_status_loader._read_last_good_cache_for_arn",
        return_value=(cached_run, cache_age),
    ):
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert result.outcome == LoadOutcome.CACHE
    assert result.run == cached_run
    assert "access denied" in result.error_message.lower()
    assert result.cache_age_seconds == pytest.approx(125.0)


def test_access_denied_no_cache_returns_error_outcome():
    """Live SFN denied AND no cache → ERROR outcome (worst case)."""
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=SFNAccessDenied("denied"),
    ), patch(
        "loaders.pipeline_status_loader._read_last_good_cache_for_arn",
        return_value=(None, None),
    ):
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert result.outcome == LoadOutcome.ERROR
    assert result.run is None
    assert "access denied" in result.error_message.lower()


# ── SFNThrottled ─────────────────────────────────────────────────────────


def test_throttled_with_cache_returns_cache_outcome():
    cached_run = _make_run()
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=SFNThrottled("rate limited"),
    ), patch(
        "loaders.pipeline_status_loader._read_last_good_cache_for_arn",
        return_value=(cached_run, 30.0),
    ):
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert result.outcome == LoadOutcome.CACHE
    assert "throttled" in result.error_message.lower()


# ── SFNNoExecutions (not treated as error) ───────────────────────────────


def test_no_executions_returns_dedicated_outcome():
    """SFNNoExecutions → NO_EXECUTIONS outcome, NEVER falls back to cache."""
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=SFNNoExecutions("never executed"),
    ):
        # Don't even hit the cache path
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert result.outcome == LoadOutcome.NO_EXECUTIONS
    assert result.run is None
    assert result.error_message == "never executed"


# ── Unknown lib exception (PipelineStatusError) ──────────────────────────


def test_unknown_pipeline_status_error_falls_back_to_cache():
    cached_run = _make_run()
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=PipelineStatusError("Unexpected boto3 weirdness"),
    ), patch(
        "loaders.pipeline_status_loader._read_last_good_cache_for_arn",
        return_value=(cached_run, 60.0),
    ):
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert result.outcome == LoadOutcome.CACHE
    assert "weirdness" in result.error_message


def test_completely_unexpected_exception_also_falls_back_to_cache():
    """Per feedback_no_silent_fails — even unanticipated exceptions get a
    specific error_message; we don't return a generic 'something went wrong'."""
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=KeyError("missing"),
    ), patch(
        "loaders.pipeline_status_loader._read_last_good_cache_for_arn",
        return_value=(None, None),
    ):
        result = read_pipeline_state_with_fallback(SAT_ARN)

    assert result.outcome == LoadOutcome.ERROR
    assert "KeyError" in result.error_message


# ── refresh_and_write_cache ──────────────────────────────────────────────


def test_refresh_skips_cache_write_when_no_arns_read_live_successfully():
    """If every ARN fails to read live, we MUST NOT overwrite the cache —
    otherwise a transient SFN outage destroys the operator's last-good state."""
    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=SFNAccessDenied("denied"),
    ), patch(
        "loaders.pipeline_status_loader._write_last_good_cache"
    ) as mock_write:
        # Option-D 2026-05-25 — refresh_and_write_cache now takes
        # (arn, role_filter) tuples so the cache warm matches the
        # filter the page will use on render.
        refresh_and_write_cache([(SAT_ARN, {"weekly"})])

    mock_write.assert_not_called()


def test_refresh_writes_cache_when_some_arns_succeed():
    fake_run = _make_run()

    call_count = [0]

    def alternating_side_effect(arn, role_filter_tuple=None, execution_arn=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return fake_run.model_dump(mode="json")
        raise SFNThrottled("throttled")

    with patch(
        "loaders.pipeline_status_loader._cached_live_read",
        side_effect=alternating_side_effect,
    ), patch(
        "loaders.pipeline_status_loader._write_last_good_cache"
    ) as mock_write:
        refresh_and_write_cache(
            [(SAT_ARN, {"weekly"}), ("arn:fake:2", {"daily"})]
        )

    # Cache write called once with the one successful ARN
    mock_write.assert_called_once()
    written = mock_write.call_args[0][0]
    assert SAT_ARN in written
    assert "arn:fake:2" not in written


# ── Cache parse robustness ──────────────────────────────────────────────


def test_cache_read_returns_none_on_empty_or_malformed_payload():
    """Degenerate S3 cache JSON must not raise — degrade gracefully."""
    with patch(
        "loaders.pipeline_status_loader.download_s3_json", return_value=None
    ):
        run, age = _read_last_good_cache_for_arn(SAT_ARN)
        assert run is None
        assert age is None


def test_cache_read_returns_none_when_arn_missing_from_cache():
    with patch(
        "loaders.pipeline_status_loader.download_s3_json",
        return_value={
            "written_utc": "2026-05-24T09:00:00Z",
            "runs": {"arn:fake:other-sf": {}},
        },
    ):
        run, age = _read_last_good_cache_for_arn(SAT_ARN)
        assert run is None
        assert age is None


def test_cache_read_returns_run_and_age_when_arn_present():
    fake_run = _make_run()
    payload = {
        "written_utc": "2026-05-24T09:00:00Z",
        "runs": {SAT_ARN: fake_run.model_dump(mode="json")},
    }
    with patch(
        "loaders.pipeline_status_loader.download_s3_json", return_value=payload
    ):
        run, age = _read_last_good_cache_for_arn(SAT_ARN)
        assert run is not None
        assert run.pretty_label == "Saturday SF"
        assert age is not None
        assert age >= 0  # any positive age relative to "now"


def test_cache_read_handles_unparseable_pipelinerun_in_cache():
    """A cache row that fails Pydantic validation degrades to (None, None)
    rather than propagating ValidationError."""
    with patch(
        "loaders.pipeline_status_loader.download_s3_json",
        return_value={
            "written_utc": "2026-05-24T09:00:00Z",
            "runs": {SAT_ARN: {"this": "is not a PipelineRun"}},
        },
    ):
        run, age = _read_last_good_cache_for_arn(SAT_ARN)
        assert run is None


# ── _write_last_good_cache ──────────────────────────────────────────────


def test_write_cache_serializes_each_run_via_model_dump():
    fake_run = _make_run()

    mock_client = MagicMock()
    with patch(
        "loaders.pipeline_status_loader.get_s3_client", return_value=mock_client
    ), patch(
        "loaders.pipeline_status_loader._research_bucket",
        return_value="alpha-engine-research",
    ):
        _write_last_good_cache({SAT_ARN: fake_run})

    mock_client.put_object.assert_called_once()
    call = mock_client.put_object.call_args
    assert call.kwargs["Bucket"] == "alpha-engine-research"
    assert call.kwargs["Key"] == "dashboard/pipeline_status_cache.json"


def test_write_cache_swallows_put_failures():
    """Cache write is best-effort — a failed put MUST NOT propagate."""
    fake_run = _make_run()

    mock_client = MagicMock()
    mock_client.put_object.side_effect = RuntimeError("S3 down")
    with patch(
        "loaders.pipeline_status_loader.get_s3_client", return_value=mock_client
    ), patch(
        "loaders.pipeline_status_loader._research_bucket",
        return_value="alpha-engine-research",
    ), patch(
        "loaders.pipeline_status_loader._record_s3_error"
    ) as mock_record:
        # MUST not raise
        _write_last_good_cache({SAT_ARN: fake_run})

    # Error IS recorded though (per feedback_no_silent_fails — surfaces in
    # the dashboard's S3 error log even if it doesn't propagate)
    mock_record.assert_called_once()


# ── derive_cycle_verdict (config#727 / #856 — artifacts, not exit code) ────


def _row(name, status, *, artifact=True):
    """Build a TaskRow; artifact=True → ArchivePageRef (artifact-bearing),
    artifact=False → ArtifactReason (substrate/notify-only)."""
    archive = (
        ArchivePageRef(page="x", artifact_label=name)
        if artifact
        else ArtifactReason(reason="substrate-only")
    )
    return TaskRow(state_name=name, status=status, archive=archive)


def _run_with(status, rows):
    return PipelineRun(
        state_machine_arn=SAT_ARN,
        pretty_label="Saturday SF",
        execution_arn=f"{SAT_ARN.replace('stateMachine', 'execution')}:t",
        execution_name="t",
        status=status,
        tasks=rows,
    )


class TestDeriveCycleVerdict:
    def test_all_artifacts_produced_but_dag_failed_is_complete(self):
        # THE BUG: SF exits FAILED at a non-artifact step but every
        # artifact-bearing state succeeded → COMPLETE.
        run = _run_with(
            RunStatus.FAILED,
            [
                _row("Research", TaskStatus.SUCCEEDED),
                _row("PredictorTraining", TaskStatus.SUCCEEDED),
                _row("Backtester", TaskStatus.SUCCEEDED),
                _row("HandleFailure", TaskStatus.SUCCEEDED, artifact=False),
            ],
        )
        cv = derive_cycle_verdict(run)
        assert cv.verdict == "COMPLETE"
        assert (cv.artifacts_produced, cv.artifacts_total) == (3, 3)
        assert cv.diverges_from_dag is True

    def test_some_missing_is_partial(self):
        run = _run_with(
            RunStatus.FAILED,
            [
                _row("Research", TaskStatus.SUCCEEDED),
                _row("PredictorTraining", TaskStatus.FAILED),
            ],
        )
        cv = derive_cycle_verdict(run)
        assert cv.verdict == "PARTIAL"
        assert (cv.artifacts_produced, cv.artifacts_total) == (1, 2)

    def test_none_produced_is_failed(self):
        run = _run_with(
            RunStatus.FAILED,
            [_row("Research", TaskStatus.FAILED), _row("Backtester", TaskStatus.NOT_RUN)],
        )
        assert derive_cycle_verdict(run).verdict == "FAILED"

    def test_clean_success_is_complete(self):
        run = _run_with(RunStatus.SUCCEEDED, [_row("Research", TaskStatus.SUCCEEDED)])
        cv = derive_cycle_verdict(run)
        assert cv.verdict == "COMPLETE"
        assert cv.diverges_from_dag is True  # COMPLETE; caller gates on dag != SUCCEEDED

    def test_substrate_only_states_do_not_count(self):
        # Only substrate/notify states present → zero artifact telemetry →
        # fall back to the DAG status (never manufacture a green).
        run = _run_with(
            RunStatus.FAILED,
            [
                _row("LibPinDriftCheck", TaskStatus.SUCCEEDED, artifact=False),
                _row("HandleFailure", TaskStatus.SUCCEEDED, artifact=False),
            ],
        )
        cv = derive_cycle_verdict(run)
        assert cv.artifacts_total == 0
        assert cv.verdict == "FAILED"

    def test_running_and_not_run_pass_through(self):
        assert derive_cycle_verdict(_run_with(RunStatus.RUNNING, [])).verdict == "RUNNING"
        assert derive_cycle_verdict(_run_with(RunStatus.NOT_RUN, [])).verdict == "NOT_RUN"
