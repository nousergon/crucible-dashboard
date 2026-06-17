"""Tests for live/loaders/system_pulse_loader.py (ROADMAP L4570e).

Covers the pure curation helpers — the public-safety layer of the System
Pulse page:
  - curate_pipeline_run keeps name/status/timing and DROPS failure_cause /
    execution names (the disclosure line for the public surface).
  - summarize_freshness reduces the heartbeat to counts only.
  - summarize_activity derives counts strictly from signals.json fields.
  - summarize_cost reduces a cost parquet frame to one spend total.

Uses the importlib-from-file pattern (mirrors test_ticker_detail.py) so the
live module loads without polluting sys.modules; live/loaders/s3_loader.py's
module-exec config read is mocked the same way. streamlit is mocked globally
by conftest.py.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import yaml  # noqa: F401  (preloaded before the open-mock window)
import pytest

_LIVE = Path(__file__).parent.parent / "live"

_FAKE_CONFIG = {
    "s3": {"research_bucket": "test-research", "trades_bucket": "test-trades"},
    "cache_ttl": {"research": 3600, "trades": 900},
}


@pytest.fixture(scope="module")
def pulse():
    """Load live/loaders/system_pulse_loader.py with live/ on sys.path.

    The module imports loaders.s3_loader (the live package sibling), whose
    @st.cache_data(ttl=_ttl(...)) decorators read the gitignored
    live/config.yaml at exec time — mocked here exactly as
    test_ticker_detail.py::_load_live_loader does.
    """
    sys.path.insert(0, str(_LIVE))
    try:
        # Drop any previously-imported top-level `loaders` package so the
        # name resolves to live/loaders for this import.
        saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.split(".")[0] == "loaders"}
        try:
            with patch("builtins.open", MagicMock()):
                with patch("yaml.safe_load", return_value=_FAKE_CONFIG):
                    spec = importlib.util.spec_from_file_location(
                        "live_system_pulse_loader_test",
                        str(_LIVE / "loaders" / "system_pulse_loader.py"),
                    )
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
            yield module
        finally:
            for k in list(sys.modules):
                if k.split(".")[0] == "loaders":
                    sys.modules.pop(k)
            sys.modules.update(saved)
    finally:
        sys.path.remove(str(_LIVE))


def _task(name="MorningEnrich", status="SUCCEEDED", start=None, duration=42.0, archive_kind=None):
    # archive_kind mirrors the lib's TaskRow.archive discriminator:
    # "archive_page_ref" = artifact-bearing (ArchivePageRef),
    # "artifact_reason" = substrate/notify-only (ArtifactReason), None = absent.
    archive = SimpleNamespace(kind=archive_kind) if archive_kind else None
    return SimpleNamespace(
        state_name=name,
        status=SimpleNamespace(value=status),
        start_utc=start,
        duration_sec=duration,
        archive=archive,
    )


def _run(**overrides):
    base = dict(
        status=SimpleNamespace(value="SUCCEEDED"),
        start_utc=datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc),
        tasks=[_task(), _task(name="PredictorInference", duration=12.5)],
        failure_cause="states.Timeout: something internal leaked",
        execution_name="weekly-2026-06-06-abc123",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCuratePipelineRun:
    def test_keeps_names_statuses_timing(self, pulse):
        out = pulse.curate_pipeline_run(_run())
        assert out["status"] == "SUCCEEDED"
        assert out["start_utc"] == "2026-06-06T09:00:00+00:00"
        assert [t["name"] for t in out["tasks"]] == ["MorningEnrich", "PredictorInference"]
        assert out["tasks"][0]["status"] == "SUCCEEDED"
        assert out["tasks"][1]["duration_sec"] == 12.5

    def test_drops_internal_detail(self, pulse):
        out = pulse.curate_pipeline_run(_run())
        flat = repr(out)
        # The disclosure line: no failure causes or execution names on the
        # public surface — they stay on the gated console.
        assert "failure_cause" not in flat
        assert "something internal" not in flat
        assert "weekly-2026-06-06-abc123" not in flat

    def test_handles_missing_tasks_and_start(self, pulse):
        out = pulse.curate_pipeline_run(_run(tasks=None, start_utc=None))
        assert out["tasks"] == []
        assert out["start_utc"] is None

    def test_plain_string_status(self, pulse):
        out = pulse.curate_pipeline_run(_run(status="running"))
        assert out["status"] == "RUNNING"


class TestCycleVerdict:
    """Cycle verdict is judged by artifacts produced, NOT the DAG terminal
    status (config#727 / #856 — the false-FAIL System Pulse fix).
    """

    def _artifact_run(self, dag_status, artifact_states):
        # artifact_states: list of (name, status) for artifact-bearing steps
        tasks = [
            _task(name=n, status=s, archive_kind="archive_page_ref")
            for n, s in artifact_states
        ]
        # plus a substrate-only notify state that should never count
        tasks.append(_task(name="HandleFailure", status="SUCCEEDED", archive_kind="artifact_reason"))
        return _run(status=SimpleNamespace(value=dag_status), tasks=tasks)

    def test_all_artifacts_produced_but_dag_failed_is_complete(self, pulse):
        # THE BUG: SF exits FAILED (Catch / DataLimitExceeded / terminal
        # notify) but every artifact-bearing state succeeded → COMPLETE.
        run = self._artifact_run(
            "FAILED",
            [("Research", "SUCCEEDED"), ("PredictorTraining", "SUCCEEDED"), ("Backtester", "SUCCEEDED")],
        )
        out = pulse.curate_pipeline_run(run)
        assert out["verdict"] == "COMPLETE"
        assert out["artifacts_produced"] == 3
        assert out["artifacts_total"] == 3
        assert out["status"] == "FAILED"  # raw DAG status preserved for transparency

    def test_some_artifacts_missing_is_partial(self, pulse):
        run = self._artifact_run(
            "FAILED",
            [("Research", "SUCCEEDED"), ("PredictorTraining", "FAILED"), ("Backtester", "SUCCEEDED")],
        )
        out = pulse.curate_pipeline_run(run)
        assert out["verdict"] == "PARTIAL"
        assert out["artifacts_produced"] == 2
        assert out["artifacts_total"] == 3

    def test_no_artifacts_produced_is_failed(self, pulse):
        run = self._artifact_run(
            "FAILED",
            [("Research", "FAILED"), ("PredictorTraining", "NOT-RUN")],
        )
        out = pulse.curate_pipeline_run(run)
        assert out["verdict"] == "FAILED"
        assert out["artifacts_produced"] == 0
        assert out["artifacts_total"] == 2

    def test_clean_success_is_complete(self, pulse):
        run = self._artifact_run(
            "SUCCEEDED",
            [("Research", "SUCCEEDED"), ("Backtester", "SUCCEEDED")],
        )
        out = pulse.curate_pipeline_run(run)
        assert out["verdict"] == "COMPLETE"

    def test_running_passes_through(self, pulse):
        run = self._artifact_run("RUNNING", [("Research", "SUCCEEDED"), ("Backtester", "RUNNING")])
        out = pulse.curate_pipeline_run(run)
        assert out["verdict"] == "RUNNING"

    def test_substrate_only_states_do_not_count(self, pulse):
        # A run whose ONLY succeeded states are substrate/notify (no
        # archive_page_ref) has zero artifact telemetry → falls back to DAG.
        run = _run(
            status=SimpleNamespace(value="FAILED"),
            tasks=[
                _task(name="LibPinDriftCheck", status="SUCCEEDED", archive_kind="artifact_reason"),
                _task(name="HandleFailure", status="SUCCEEDED", archive_kind="artifact_reason"),
            ],
        )
        out = pulse.curate_pipeline_run(run)
        assert out["artifacts_total"] == 0
        assert out["verdict"] == "FAILED"  # no evidence → trust DAG status

    def test_no_archive_telemetry_falls_back_to_dag_status(self, pulse):
        # Curated fixtures / older runs without archive tags must not
        # manufacture a verdict from absent evidence.
        out = pulse.curate_pipeline_run(_run())  # default tasks have no archive
        assert out["artifacts_total"] == 0
        assert out["verdict"] == "COMPLETE"  # dag SUCCEEDED → COMPLETE
        out_failed = pulse.curate_pipeline_run(_run(status=SimpleNamespace(value="FAILED")))
        assert out_failed["verdict"] == "FAILED"

    def test_not_run_passes_through(self, pulse):
        out = pulse.curate_pipeline_run(_run(status=SimpleNamespace(value="NOT-RUN"), tasks=[]))
        assert out["verdict"] == "NOT_RUN"


class TestSummarizeFreshness:
    def test_counts(self, pulse):
        hb = {
            "last_run": "2026-06-09T21:45:33+00:00",
            "n_entries_checked": 55,
            "counts": {
                "fresh": 10,
                "grace_period": 36,
                "stale": 6,
                "missing": 0,
                "probe_failed": 3,
            },
        }
        out = pulse.summarize_freshness(hb)
        assert out == {
            "n_total": 55,
            "within_sla": 46,
            "stale": 6,
            "missing": 0,
            "probe_failed": 3,
            "last_run": "2026-06-09T21:45:33+00:00",
        }

    def test_none_and_empty(self, pulse):
        assert pulse.summarize_freshness(None) is None
        assert pulse.summarize_freshness({}) is None
        assert pulse.summarize_freshness({"counts": {}}) is None

    def test_total_falls_back_to_count_sum(self, pulse):
        out = pulse.summarize_freshness({"counts": {"fresh": 3, "stale": 1}})
        assert out["n_total"] == 4
        assert out["within_sla"] == 3


class TestSummarizeActivity:
    def test_counts_from_signals(self, pulse):
        sig = {
            "date": "2026-06-05",
            "market_regime": "neutral",
            "universe": [{"ticker": "A"}, {"ticker": "B"}],
            "population": [{"ticker": "A"}],
            "buy_candidates": [],
        }
        out = pulse.summarize_activity(sig)
        assert out == {
            "date": "2026-06-05",
            "regime": "neutral",
            "tracked": 2,
            "population": 1,
            "buy_candidates": 0,
        }

    def test_none_signals(self, pulse):
        assert pulse.summarize_activity(None) is None

    def test_missing_fields_count_zero(self, pulse):
        out = pulse.summarize_activity({"date": "2026-06-05"})
        assert out["tracked"] == 0
        assert out["population"] == 0


class TestUptimeBucket:
    """Regression: uptime records live in the RESEARCH bucket.

    The loader read the executor bucket (empty uptime/ prefix) — the public
    Uptime page silently rendered its empty state in production. Locks the
    bucket choice (L4570e finding, 2026-06-09).
    """

    def test_load_uptime_history_lists_research_bucket(self):
        sys.path.insert(0, str(_LIVE))
        try:
            saved = {
                k: sys.modules.pop(k)
                for k in list(sys.modules)
                if k.split(".")[0] == "loaders"
            }
            try:
                with patch("builtins.open", MagicMock()):
                    with patch("yaml.safe_load", return_value=_FAKE_CONFIG):
                        spec = importlib.util.spec_from_file_location(
                            "live_s3_loader_uptime_test",
                            str(_LIVE / "loaders" / "s3_loader.py"),
                        )
                        s3l = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(s3l)
                client = MagicMock()
                client.list_objects_v2.return_value = {"Contents": []}
                with patch.object(s3l, "get_s3_client", return_value=client):
                    assert s3l.load_uptime_history(max_sessions=5) == []
                client.list_objects_v2.assert_called_once_with(
                    Bucket="test-research", Prefix="uptime/"
                )
            finally:
                for k in list(sys.modules):
                    if k.split(".")[0] == "loaders":
                        sys.modules.pop(k)
                sys.modules.update(saved)
        finally:
            sys.path.remove(str(_LIVE))


class TestSummarizeCost:
    def test_total_and_calls(self, pulse):
        df = pd.DataFrame({"cost_usd": [1.25, 2.50, 0.25]})
        out = pulse.summarize_cost(df, "2026-06-06")
        assert out == {"capture_date": "2026-06-06", "total_usd": 4.0, "n_calls": 3}

    def test_non_numeric_rows_ignored(self, pulse):
        df = pd.DataFrame({"cost_usd": [1.0, "bogus", None]})
        out = pulse.summarize_cost(df, "2026-06-06")
        assert out["total_usd"] == 1.0

    def test_empty_or_missing_column(self, pulse):
        assert pulse.summarize_cost(None, "d") is None
        assert pulse.summarize_cost(pd.DataFrame(), "d") is None
        assert pulse.summarize_cost(pd.DataFrame({"other": [1]}), "d") is None
