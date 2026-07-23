"""Render-smoke tests for the weekly-SF live progress strip on
views/48_Fleet_Status.py (config-I2966).

Exec-loads the page with streamlit mocked (mirrors
tests/test_fleet_status_page.py's ``rendered_page`` fixture pattern) and
stubs ``gather_fleet_inputs`` + ``rag_ingestion_progress`` directly, so no
AWS/S3/network dependency. Verifies:

  - the strip is ABSENT when no weekly execution is RUNNING (no dead
    chrome — the issue's explicit acceptance criterion)
  - the strip IS rendered, with the RAGIngestion chip's inner-step text,
    when the weekly snapshot is RUNNING
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

PAGE = REPO_ROOT / "views" / "48_Fleet_Status.py"

from nousergon_lib.pipeline_status import TaskStatus  # noqa: E402
from nousergon_lib.pipeline_status.read import TaskRow  # noqa: E402
from nousergon_lib.pipeline_status.registry import ArtifactReason  # noqa: E402


def _task(state_name, status, start=None) -> TaskRow:
    return TaskRow(
        state_name=state_name, status=status, start_utc=start,
        archive=ArtifactReason(reason="x"),
    )


def _inputs_with_weekly_running(now):
    from fleet_status import FleetInputs, GroomSnapshot, PipelineSnapshot

    tasks = (
        _task("MorningEnrich", TaskStatus.SUCCEEDED),
        _task("DataPhase1", TaskStatus.SUCCEEDED),
        _task("RAGIngestion", TaskStatus.RUNNING, start=now - timedelta(minutes=8)),
    )
    snap_weekly = PipelineSnapshot(
        status="RUNNING",
        started_at=now - timedelta(hours=1),
        current_state="RAGIngestion",
        tasks=tasks,
    )
    snap_idle = PipelineSnapshot(status="SUCCEEDED", verdict="COMPLETE")
    return FleetInputs(
        now=now, is_trading_day=False,
        trading_instance_state="stopped",
        pipelines={"weekly": snap_weekly, "preopen": snap_idle, "postclose": snap_idle},
        heartbeat={"last_run": now.isoformat(), "alerts_enabled": True},
        check_results={"run_at": now.isoformat(), "results": []},
        groom=GroomSnapshot(),
    )


def _inputs_with_nothing_running(now):
    from fleet_status import FleetInputs, GroomSnapshot, PipelineSnapshot

    snap_idle = PipelineSnapshot(status="SUCCEEDED", verdict="COMPLETE")
    return FleetInputs(
        now=now, is_trading_day=False,
        trading_instance_state="stopped",
        pipelines={"weekly": snap_idle, "preopen": snap_idle, "postclose": snap_idle},
        heartbeat={"last_run": now.isoformat(), "alerts_enabled": True},
        check_results={"run_at": now.isoformat(), "results": []},
        groom=GroomSnapshot(),
    )


class _ColumnsMock(MagicMock):
    """Returns exactly N usable MagicMock column-context-managers for
    whatever N the page requests — unlike the fixed-5 mock in
    test_fleet_status_page.py, the strip requests variable widths
    (2 for the branch lanes, up to 9 for the backbone, etc)."""

    def __call__(self, spec):
        n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
        return [MagicMock() for _ in range(max(n, 1))]


def _render(now, inputs_fn, rag_progress=None):
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    mock_st.fragment = lambda **kw: (lambda f: f)
    mock_st.columns = _ColumnsMock()
    mock_st.expander = MagicMock()
    mock_st.expander.return_value.__enter__ = MagicMock(return_value=None)
    mock_st.expander.return_value.__exit__ = MagicMock(return_value=False)

    import loaders.fleet_status_loader as fsl

    saved_st = sys.modules.get("streamlit")
    sys.modules["streamlit"] = mock_st
    saved_gather = fsl.gather_fleet_inputs
    saved_rag = fsl.rag_ingestion_progress
    fsl.gather_fleet_inputs = lambda: inputs_fn(now)
    fsl.rag_ingestion_progress = lambda run_date: rag_progress
    try:
        spec = importlib.util.spec_from_file_location("fleet_status_page_strip", PAGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        fsl.gather_fleet_inputs = saved_gather
        fsl.rag_ingestion_progress = saved_rag
        if saved_st is not None:
            sys.modules["streamlit"] = saved_st
    return mock_st


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


class TestStripAbsence:
    def test_strip_absent_when_nothing_running(self):
        mock_st = _render(NOW, _inputs_with_nothing_running)
        subheaders = [c.args[0] for c in mock_st.subheader.call_args_list]
        assert not any("live progress" in s for s in subheaders)


class TestStripPresence:
    def test_strip_renders_when_weekly_running(self):
        mock_st = _render(NOW, _inputs_with_weekly_running)
        subheaders = [c.args[0] for c in mock_st.subheader.call_args_list]
        assert any("live progress" in s for s in subheaders)

    def test_ragingestion_inner_step_rendered(self):
        from fleet_status import RagIngestionProgress

        progress = RagIngestionProgress(
            step=5, of=10, label="news",
            updated_at=(NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        mock_st = _render(NOW, _inputs_with_weekly_running, rag_progress=progress)
        captions = [c.args[0] for c in mock_st.caption.call_args_list]
        assert any("step 5/10: news" in c for c in captions)

    def test_deep_link_to_pipeline_status_present(self):
        mock_st = _render(NOW, _inputs_with_weekly_running)
        captions = [c.args[0] for c in mock_st.caption.call_args_list]
        assert any("/pipeline-status" in c for c in captions)
