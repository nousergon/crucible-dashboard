"""Unit tests for the weekly-SF live progress strip (config-I2966).

Covers the pure projection logic in ``fleet_status.build_weekly_sf_strip``
(done/running/pending/failed classification, Branch A/B lane assignment,
RAGIngestion inner-step + staleness enrichment) with a frozen clock — no
AWS/streamlit dependency, matching this module's existing discipline for
every other resolver (see tests/test_fleet_status.py).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nousergon_lib.pipeline_status import TaskStatus  # noqa: E402
from nousergon_lib.pipeline_status.read import TaskRow  # noqa: E402
from nousergon_lib.pipeline_status.registry import ArtifactReason  # noqa: E402

from fleet_status import (  # noqa: E402
    RAG_PROGRESS_STALE_MINUTES,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    WEEKLY_SF_BRANCH_A_STEPS,
    WEEKLY_SF_BRANCH_B_STEPS,
    WEEKLY_SF_STRIP_STATES,
    RagIngestionProgress,
    build_weekly_sf_strip,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _task(state_name, status, start=None, reason="x") -> TaskRow:
    return TaskRow(
        state_name=state_name,
        status=status,
        start_utc=start,
        archive=ArtifactReason(reason=reason),
    )


class TestClassification:
    def test_absent_state_is_pending(self):
        steps = build_weekly_sf_strip([], now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["MorningEnrich"].state == STEP_PENDING
        assert by_name["MorningEnrich"].elapsed_sec is None

    def test_succeeded_is_done(self):
        tasks = [_task("MorningEnrich", TaskStatus.SUCCEEDED)]
        steps = build_weekly_sf_strip(tasks, now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["MorningEnrich"].state == STEP_DONE

    def test_skipped_is_done_not_pending(self):
        """A Choice-branched-past state (e.g. ChallengerShadow when
        observe-mode is off) must never show as perpetually pending —
        it's behind us, not ahead."""
        tasks = [_task("ChallengerShadow", TaskStatus.SKIPPED)]
        steps = build_weekly_sf_strip(tasks, now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["ChallengerShadow"].state == STEP_DONE

    def test_running_computes_elapsed(self):
        start = NOW - timedelta(minutes=12)
        tasks = [_task("RAGIngestion", TaskStatus.RUNNING, start=start)]
        steps = build_weekly_sf_strip(tasks, now=NOW)
        by_name = {s.state_name: s for s in steps}
        step = by_name["RAGIngestion"]
        assert step.state == STEP_RUNNING
        assert step.elapsed_sec == pytest.approx(720.0)

    def test_failed_timed_out_aborted_all_map_to_failed(self):
        for status in (TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.ABORTED):
            tasks = [_task("Evaluator", status)]
            steps = build_weekly_sf_strip(tasks, now=NOW)
            by_name = {s.state_name: s for s in steps}
            assert by_name["Evaluator"].state == STEP_FAILED, status

    def test_not_run_is_pending(self):
        tasks = [_task("Director", TaskStatus.NOT_RUN)]
        steps = build_weekly_sf_strip(tasks, now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["Director"].state == STEP_PENDING


class TestLanes:
    def test_ragingestion_is_branch_a(self):
        steps = build_weekly_sf_strip([], now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].lane == "Branch A"

    def test_predictor_training_is_branch_b(self):
        steps = build_weekly_sf_strip([], now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["PredictorTraining"].lane == "Branch B"

    def test_morning_enrich_has_no_lane(self):
        steps = build_weekly_sf_strip([], now=NOW)
        by_name = {s.state_name: s for s in steps}
        assert by_name["MorningEnrich"].lane is None

    def test_branch_a_and_b_are_disjoint(self):
        assert not (set(WEEKLY_SF_BRANCH_A_STEPS) & set(WEEKLY_SF_BRANCH_B_STEPS))

    def test_every_strip_state_appears_exactly_once(self):
        assert len(WEEKLY_SF_STRIP_STATES) == len(set(WEEKLY_SF_STRIP_STATES))


class TestRagEnrichment:
    def test_rag_inner_step_only_populated_when_running(self):
        progress = RagIngestionProgress(
            step=5, of=10, label="news",
            started_at="2026-07-25T09:00:00Z",
            updated_at="2026-07-25T11:55:00Z",
        )
        tasks = [_task("RAGIngestion", TaskStatus.RUNNING, start=NOW - timedelta(minutes=5))]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=progress)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].rag_inner_step == "step 5/10: news"

    def test_rag_inner_step_absent_when_not_running(self):
        progress = RagIngestionProgress(step=5, of=10, label="news")
        tasks = [_task("RAGIngestion", TaskStatus.SUCCEEDED)]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=progress)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].rag_inner_step is None

    def test_rag_inner_step_absent_when_no_progress_artifact(self):
        tasks = [_task("RAGIngestion", TaskStatus.RUNNING, start=NOW)]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=None)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].rag_inner_step is None
        assert by_name["RAGIngestion"].rag_stale is False

    def test_fresh_updated_at_is_not_stale(self):
        progress = RagIngestionProgress(
            step=5, of=10, label="news",
            updated_at=(NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        tasks = [_task("RAGIngestion", TaskStatus.RUNNING, start=NOW)]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=progress)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].rag_stale is False

    def test_stale_updated_at_past_45_min_renders_amber(self):
        stale_time = NOW - timedelta(minutes=RAG_PROGRESS_STALE_MINUTES + 1)
        progress = RagIngestionProgress(
            step=5, of=10, label="news",
            updated_at=stale_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        tasks = [_task("RAGIngestion", TaskStatus.RUNNING, start=NOW)]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=progress)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].rag_stale is True

    def test_exactly_45_min_boundary_is_not_yet_stale(self):
        boundary = NOW - timedelta(minutes=RAG_PROGRESS_STALE_MINUTES)
        progress = RagIngestionProgress(
            step=5, of=10, label="news",
            updated_at=boundary.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        tasks = [_task("RAGIngestion", TaskStatus.RUNNING, start=NOW)]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=progress)
        by_name = {s.state_name: s for s in steps}
        assert by_name["RAGIngestion"].rag_stale is False

    def test_only_ragingestion_chip_carries_rag_fields(self):
        progress = RagIngestionProgress(step=5, of=10, label="news")
        tasks = [
            _task("RAGIngestion", TaskStatus.RUNNING, start=NOW),
            _task("MorningEnrich", TaskStatus.RUNNING, start=NOW),
        ]
        steps = build_weekly_sf_strip(tasks, now=NOW, rag_progress=progress)
        by_name = {s.state_name: s for s in steps}
        assert by_name["MorningEnrich"].rag_inner_step is None
