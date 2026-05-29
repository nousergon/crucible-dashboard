"""Unit tests for ``loaders.eval_loader`` (PR 4d)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Stub loaders.s3_loader BEFORE importing eval_loader. The real
# s3_loader runs load_config() at import time which fails outside of
# a configured dashboard env. Same pattern as tests/test_db_loader.py.
sys.modules.setdefault("streamlit", MagicMock())

_s3_loader_stub = MagicMock()
_s3_loader_stub.get_s3_client = MagicMock()
_s3_loader_stub._fetch_s3_json = MagicMock()
_s3_loader_stub._research_bucket = lambda: "test-bucket"
sys.modules["loaders.s3_loader"] = _s3_loader_stub

from loaders.eval_loader import (  # noqa: E402
    _explode_eval_artifact,
    load_eval_artifacts,
)


def _eval_artifact_payload(
    *,
    judged_agent_id: str = "ic_cio",
    judge_model: str = "claude-haiku-4-5",
    rubric_version: str = "1.0.0",
    run_id: str = "2026-05-09",
    scores: list[tuple[str, int]] | None = None,
) -> dict:
    scores = scores or [
        ("decision_coherence", 4),
        ("rationale_quality", 3),
    ]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "timestamp": "2026-05-09T22:30:00.000Z",
        "judged_agent_id": judged_agent_id,
        "judged_artifact_s3_key": f"decision_artifacts/2026/05/09/{judged_agent_id}/{run_id}.json",
        "rubric_id": f"eval_rubric_{judged_agent_id.split(':')[0]}",
        "rubric_version": rubric_version,
        "judge_model": judge_model,
        "dimension_scores": [
            {"dimension": d, "score": s, "reasoning": f"r-{d}"}
            for d, s in scores
        ],
        "overall_reasoning": "ok",
    }


# ── _explode_eval_artifact ────────────────────────────────────────────────


class TestExplodeEvalArtifact:
    def test_one_row_per_dimension(self):
        artifact = _eval_artifact_payload(scores=[
            ("d1", 4), ("d2", 5), ("d3", 3),
        ])
        rows = _explode_eval_artifact(artifact, "2026-05-09")
        assert len(rows) == 3
        assert [r["criterion"] for r in rows] == ["d1", "d2", "d3"]
        assert [r["score"] for r in rows] == [4, 5, 3]
        assert all(r["judged_agent_id"] == "ic_cio" for r in rows)
        assert all(r["eval_date"] == "2026-05-09" for r in rows)

    def test_metadata_propagated_to_each_row(self):
        artifact = _eval_artifact_payload(
            judged_agent_id="sector_quant:technology",
            judge_model="claude-sonnet-4-6",
            rubric_version="1.2.0",
            run_id="run-test-1",
        )
        rows = _explode_eval_artifact(artifact, "2026-05-09")
        for row in rows:
            assert row["judge_model"] == "claude-sonnet-4-6"
            assert row["rubric_version"] == "1.2.0"
            assert row["run_id"] == "run-test-1"
            assert row["overall_reasoning"] == "ok"

    def test_empty_dimension_scores_returns_empty(self):
        artifact = _eval_artifact_payload()
        artifact["dimension_scores"] = []
        rows = _explode_eval_artifact(artifact, "2026-05-09")
        assert rows == []

    def test_missing_optional_fields_default_safely(self):
        # Defensive: an artifact that's missing optional metadata
        # shouldn't raise — it's better to surface partial data on
        # the dashboard than to crash the page.
        rows = _explode_eval_artifact({
            "dimension_scores": [{"dimension": "d1", "score": 4, "reasoning": "r"}]
        }, "2026-05-09")
        assert len(rows) == 1
        assert rows[0]["judge_model"] == ""
        assert rows[0]["rubric_version"] == ""


# ── load_eval_artifacts ───────────────────────────────────────────────────


@pytest.fixture
def mock_s3():
    """Stub the S3 client paginator to return controllable list_objects_v2
    + CommonPrefixes responses, plus _fetch_s3_json for artifact downloads."""
    paginator = MagicMock()
    client = MagicMock()
    client.get_paginator.return_value = paginator
    _s3_loader_stub.get_s3_client.return_value = client
    yield {
        "client": client,
        "paginator": paginator,
        "fetch_json": _s3_loader_stub._fetch_s3_json,
    }
    # Reset between tests so a stub from one test doesn't leak.
    _s3_loader_stub._fetch_s3_json.reset_mock(side_effect=True)
    paginator.paginate.reset_mock(side_effect=True)


def _setup_paginator_responses(paginator, *, dates: list[str], keys_by_date: dict[str, list[str]]):
    """Configure paginator.paginate to return:
       - first call (Delimiter='/') → CommonPrefixes for each date
       - subsequent calls (per-date list_keys) → Contents for that date.
    """
    date_page = {
        "CommonPrefixes": [
            {"Prefix": f"decision_artifacts/_eval/{d}/"} for d in dates
        ],
    }
    per_date_pages = {
        d: [{"Contents": [{"Key": k} for k in keys_by_date[d]]}]
        for d in dates
    }

    call_count = {"n": 0}

    def fake_paginate(**kwargs):
        delimiter = kwargs.get("Delimiter")
        if delimiter == "/":
            return iter([date_page])
        prefix = kwargs.get("Prefix", "")
        # Extract date from the prefix
        date_str = prefix.replace("decision_artifacts/_eval/", "").rstrip("/")
        return iter(per_date_pages.get(date_str, [{"Contents": []}]))

    paginator.paginate.side_effect = fake_paginate


class TestLoadEvalArtifacts:
    def test_empty_corpus_returns_empty_dataframe_with_schema(self, mock_s3):
        _setup_paginator_responses(mock_s3["paginator"], dates=[], keys_by_date={})
        df = load_eval_artifacts(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 9),
            bucket="test-bucket",
        )
        assert df.empty
        # Must still expose the expected columns so the page can build
        # plot specs without conditionals on schema.
        assert set(df.columns) >= {
            "eval_date", "judged_agent_id", "criterion", "score",
            "judge_model", "rubric_version",
        }

    def test_happy_path_one_artifact_one_date(self, mock_s3):
        date_str = "2026-05-09"
        key = f"decision_artifacts/_eval/{date_str}/ic_cio/r1.claude-haiku-4-5.json"
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=[date_str],
            keys_by_date={date_str: [key]},
        )
        mock_s3["fetch_json"].side_effect = lambda b, k: _eval_artifact_payload(
            scores=[("d1", 4), ("d2", 3)],
        )

        df = load_eval_artifacts(
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 9),
            bucket="test-bucket",
        )
        assert len(df) == 2
        assert set(df["criterion"]) == {"d1", "d2"}
        # Sorted by criterion ascending → d1(4) before d2(3).
        assert df["criterion"].tolist() == ["d1", "d2"]
        assert df["score"].tolist() == [4, 3]

    def test_filters_dates_outside_window(self, mock_s3):
        in_window = "2026-05-09"
        too_early = "2026-04-01"
        too_late = "2026-06-15"
        keys = {
            in_window: [f"decision_artifacts/_eval/{in_window}/ic_cio/r1.claude-haiku-4-5.json"],
            too_early: [f"decision_artifacts/_eval/{too_early}/ic_cio/r0.claude-haiku-4-5.json"],
            too_late: [f"decision_artifacts/_eval/{too_late}/ic_cio/r2.claude-haiku-4-5.json"],
        }
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=[too_early, in_window, too_late],
            keys_by_date=keys,
        )
        mock_s3["fetch_json"].side_effect = lambda b, k: _eval_artifact_payload()

        df = load_eval_artifacts(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            bucket="test-bucket",
        )
        # Only the in-window date contributes rows.
        assert df["eval_date"].dt.strftime("%Y-%m-%d").unique().tolist() == [in_window]

    def test_two_judge_models_for_same_artifact_keep_separate(self, mock_s3):
        """When eval-judge writes both Haiku and Sonnet for the same
        artifact, both files surface in the loader's output as
        separate rows distinguished by judge_model."""
        date_str = "2026-05-09"
        haiku_key = f"decision_artifacts/_eval/{date_str}/ic_cio/r1.claude-haiku-4-5.json"
        sonnet_key = f"decision_artifacts/_eval/{date_str}/ic_cio/r1.claude-sonnet-4-6.json"
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=[date_str],
            keys_by_date={date_str: [haiku_key, sonnet_key]},
        )

        def fetch(_b, key):
            if "haiku" in key:
                return _eval_artifact_payload(judge_model="claude-haiku-4-5")
            return _eval_artifact_payload(judge_model="claude-sonnet-4-6")

        mock_s3["fetch_json"].side_effect = fetch

        df = load_eval_artifacts(
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 9),
            bucket="test-bucket",
        )
        assert set(df["judge_model"]) == {"claude-haiku-4-5", "claude-sonnet-4-6"}
        # Same agent_id, same criteria — 2 dimensions × 2 judges = 4 rows
        assert len(df) == 4

    def test_skips_artifacts_that_failed_to_fetch(self, mock_s3):
        """If _fetch_s3_json returns None (404 or transient), skip
        that artifact rather than crashing the whole page."""
        date_str = "2026-05-09"
        good_key = f"decision_artifacts/_eval/{date_str}/ic_cio/r1.claude-haiku-4-5.json"
        bad_key = f"decision_artifacts/_eval/{date_str}/ic_cio/r2.claude-haiku-4-5.json"
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=[date_str],
            keys_by_date={date_str: [good_key, bad_key]},
        )

        def fetch(_b, key):
            if "r2" in key:
                return None
            return _eval_artifact_payload(scores=[("d1", 4)])

        mock_s3["fetch_json"].side_effect = fetch

        df = load_eval_artifacts(
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 9),
            bucket="test-bucket",
        )
        assert len(df) == 1
        assert df.iloc[0]["run_id"] == "2026-05-09"


# ── Judge calibration review (ROADMAP L480 SOTA reframe) ──────────────────


from loaders.eval_loader import (  # noqa: E402
    _score_uncertainty,
    _review_id,
    load_recent_eval_artifacts_for_review,
    save_calibration_review,
)


@pytest.fixture(autouse=True)
def _clear_st_cache():
    """Streamlit's @st.cache_data memoizes by args; clear between tests
    so a stubbed paginator from one test doesn't leak into another.
    """
    try:
        import streamlit as _st
        _st.cache_data.clear()
    except Exception:
        pass
    yield
    try:
        import streamlit as _st
        _st.cache_data.clear()
    except Exception:
        pass


class TestScoreUncertainty:
    def test_lower_distance_higher_priority(self):
        near = _score_uncertainty([{"score": 3}, {"score": 3}, {"score": 3}])
        edge = _score_uncertainty([{"score": 1}, {"score": 5}, {"score": 1}])
        assert near < edge

    def test_empty_dimensions_back_of_queue(self):
        assert _score_uncertainty([]) == float("inf")
        assert _score_uncertainty(None) == float("inf")  # type: ignore[arg-type]

    def test_missing_score_skipped_not_crashed(self):
        result = _score_uncertainty([
            {"score": 3}, {"score": None}, {"score": 4},
        ])
        # Mean of |3-3| + |4-3| = (0 + 1)/2 = 0.5; the None row drops.
        assert result == pytest.approx(0.5)


class TestLoadRecentEvalArtifactsForReview:
    def test_ranks_by_uncertainty_lowest_first(self, mock_s3):
        certain_art = _eval_artifact_payload(scores=[("d1", 1), ("d2", 5)])
        uncertain_art = _eval_artifact_payload(scores=[("d1", 3), ("d2", 3)])
        certain_art["judged_agent_id"] = "ic_cio"
        uncertain_art["judged_agent_id"] = "sector_quant:tech"
        certain_art["run_id"] = "C"
        uncertain_art["run_id"] = "U"

        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=["2026-05-22"],
            keys_by_date={"2026-05-22": ["a.json", "b.json"]},
        )

        def fetch(_b, key):
            return certain_art if key == "a.json" else uncertain_art

        mock_s3["fetch_json"].side_effect = fetch

        batch = load_recent_eval_artifacts_for_review(
            n=2, lookback_days=365, bucket="test-bucket",
        )
        assert batch[0]["run_id"] == "U"
        assert batch[1]["run_id"] == "C"

    def test_excludes_judge_skipped_artifacts(self, mock_s3):
        skipped = _eval_artifact_payload()
        skipped["judge_skip_reason"] = "precluded_by_empty_upstream"
        real = _eval_artifact_payload(scores=[("d1", 3)])

        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=["2026-05-22"],
            keys_by_date={"2026-05-22": ["a.json", "b.json"]},
        )
        mock_s3["fetch_json"].side_effect = lambda _b, k: (
            skipped if k == "a.json" else real
        )

        batch = load_recent_eval_artifacts_for_review(
            n=5, lookback_days=365, bucket="test-bucket",
        )
        assert len(batch) == 1
        assert "judge_skip_reason" not in batch[0]

    def test_reviewed_ids_drop_out_of_queue(self, mock_s3):
        art = _eval_artifact_payload()
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=["2026-05-22"],
            keys_by_date={"2026-05-22": ["a.json"]},
        )
        mock_s3["fetch_json"].side_effect = lambda _b, _k: art

        rid = _review_id(
            "2026-05-22",
            art["judged_agent_id"],
            art["run_id"],
            art["judge_model"],
        )
        batch = load_recent_eval_artifacts_for_review(
            n=5, lookback_days=365, bucket="test-bucket",
            reviewed_ids=(rid,),
        )
        assert batch == []


class TestSaveCalibrationReview:
    def test_missing_review_id_rejected(self, mock_s3):
        ok = save_calibration_review({"foo": "bar"}, bucket="test-bucket")
        assert ok is False
        mock_s3["client"].put_object.assert_not_called()

    def test_first_write_uploads_jsonl_line(self, mock_s3):
        mock_s3["client"].get_object.side_effect = Exception("NoSuchKey")
        ok = save_calibration_review(
            {
                "review_id": "2026-05-22__ic_cio__r1__haiku",
                "per_dimension": [
                    {"dimension": "d1", "blind_score": 3, "llm_score": 4, "final_score": 3},
                ],
            },
            bucket="test-bucket",
        )
        assert ok is True
        mock_s3["client"].put_object.assert_called_once()
        body = mock_s3["client"].put_object.call_args.kwargs["Body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        import json as _json
        parsed = _json.loads(body.strip())
        assert parsed["review_id"] == "2026-05-22__ic_cio__r1__haiku"
        assert "reviewed_at_utc" in parsed

    def test_append_preserves_prior_lines(self, mock_s3):
        prior = b'{"review_id": "old-id"}\n'
        mock_s3["client"].get_object.side_effect = None
        mock_s3["client"].get_object.return_value = {
            "Body": MagicMock(read=lambda: prior)
        }
        ok = save_calibration_review(
            {"review_id": "new-id"}, bucket="test-bucket",
        )
        assert ok is True
        body = mock_s3["client"].put_object.call_args.kwargs["Body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        import json as _json
        lines = [_json.loads(ln) for ln in body.strip().split("\n") if ln]
        assert len(lines) == 2
        assert lines[0]["review_id"] == "old-id"
        assert lines[1]["review_id"] == "new-id"

    def test_failure_returns_false_no_raise(self, mock_s3):
        mock_s3["client"].get_object.side_effect = Exception("NoSuchKey")
        mock_s3["client"].put_object.side_effect = RuntimeError("S3 down")
        ok = save_calibration_review({"review_id": "x"}, bucket="test-bucket")
        assert ok is False


# ── Judge spot-check (ROADMAP L480 2026-05-29 re-scope) ───────────────────

from loaders.eval_loader import (  # noqa: E402
    load_judged_artifact,
    load_recent_evals_for_spotcheck,
    save_spotcheck_flag,
)


class TestLoadRecentEvalsForSpotcheck:
    def test_newest_date_first_then_uncertainty(self, mock_s3):
        old_art = _eval_artifact_payload(scores=[("d1", 3), ("d2", 3)])
        new_certain = _eval_artifact_payload(scores=[("d1", 1), ("d2", 5)])
        new_uncertain = _eval_artifact_payload(scores=[("d1", 3), ("d2", 3)])
        old_art["run_id"] = "OLD"
        new_certain["run_id"] = "NC"
        new_uncertain["run_id"] = "NU"

        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=["2026-05-15", "2026-05-22"],
            keys_by_date={
                "2026-05-15": ["old.json"],
                "2026-05-22": ["nc.json", "nu.json"],
            },
        )

        def fetch(_b, key):
            return {
                "old.json": old_art,
                "nc.json": new_certain,
                "nu.json": new_uncertain,
            }[key]

        mock_s3["fetch_json"].side_effect = fetch

        batch = load_recent_evals_for_spotcheck(
            n=5, lookback_days=365, bucket="test-bucket",
        )
        # Newest date (05-22) first; within it the uncertain (midpoint)
        # call ranks ahead of the certain one. Old date trails.
        assert [a["run_id"] for a in batch] == ["NU", "NC", "OLD"]

    def test_excludes_judge_skipped(self, mock_s3):
        skipped = _eval_artifact_payload()
        skipped["judge_skip_reason"] = "degenerate_input"
        real = _eval_artifact_payload(scores=[("d1", 3)])
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=["2026-05-22"],
            keys_by_date={"2026-05-22": ["a.json", "b.json"]},
        )
        mock_s3["fetch_json"].side_effect = lambda _b, k: (
            skipped if k == "a.json" else real
        )
        batch = load_recent_evals_for_spotcheck(
            n=5, lookback_days=365, bucket="test-bucket",
        )
        assert len(batch) == 1
        assert "judge_skip_reason" not in batch[0]

    def test_respects_n_cap(self, mock_s3):
        _setup_paginator_responses(
            mock_s3["paginator"],
            dates=["2026-05-22"],
            keys_by_date={"2026-05-22": ["a.json", "b.json", "c.json"]},
        )
        mock_s3["fetch_json"].side_effect = lambda _b, _k: _eval_artifact_payload(
            scores=[("d1", 3)]
        )
        batch = load_recent_evals_for_spotcheck(
            n=2, lookback_days=365, bucket="test-bucket",
        )
        assert len(batch) == 2


class TestLoadJudgedArtifact:
    def test_none_key_returns_none(self, mock_s3):
        assert load_judged_artifact(None, bucket="test-bucket") is None
        mock_s3["fetch_json"].assert_not_called()

    def test_hydrates_decision_artifact(self, mock_s3):
        mock_s3["fetch_json"].side_effect = lambda _b, _k: {
            "agent_output": {"ranked_picks": [{"ticker": "AAPL"}]},
            "input_data_snapshot": {"prices": "..."},
        }
        out = load_judged_artifact(
            "decision_artifacts/2026/05/22/sector_quant/r1.json",
            bucket="test-bucket",
        )
        assert out["agent_output"]["ranked_picks"][0]["ticker"] == "AAPL"

    def test_unfetchable_returns_none(self, mock_s3):
        mock_s3["fetch_json"].side_effect = lambda _b, _k: None
        assert load_judged_artifact("missing.json", bucket="test-bucket") is None


class TestSaveSpotcheckFlag:
    def test_missing_id_rejected(self, mock_s3):
        ok = save_spotcheck_flag({"verdict": "looks_right"}, bucket="test-bucket")
        assert ok is False
        mock_s3["client"].put_object.assert_not_called()

    def test_first_write_stamps_and_uploads(self, mock_s3):
        mock_s3["client"].get_object.side_effect = Exception("NoSuchKey")
        ok = save_spotcheck_flag(
            {"spotcheck_id": "2026-05-22__ic_cio__r1__haiku", "verdict": "looks_wrong"},
            bucket="test-bucket",
        )
        assert ok is True
        body = mock_s3["client"].put_object.call_args.kwargs["Body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        import json as _json
        parsed = _json.loads(body.strip())
        assert parsed["verdict"] == "looks_wrong"
        assert "flagged_at_utc" in parsed

    def test_append_preserves_prior(self, mock_s3):
        mock_s3["client"].get_object.side_effect = None
        mock_s3["client"].get_object.return_value = {
            "Body": MagicMock(read=lambda: b'{"spotcheck_id": "old"}\n')
        }
        ok = save_spotcheck_flag(
            {"spotcheck_id": "new", "verdict": "looks_right"}, bucket="test-bucket",
        )
        assert ok is True
        body = mock_s3["client"].put_object.call_args.kwargs["Body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        import json as _json
        lines = [_json.loads(ln) for ln in body.strip().split("\n") if ln]
        assert [ln["spotcheck_id"] for ln in lines] == ["old", "new"]

    def test_failure_returns_false_no_raise(self, mock_s3):
        mock_s3["client"].get_object.side_effect = Exception("NoSuchKey")
        mock_s3["client"].put_object.side_effect = RuntimeError("S3 down")
        ok = save_spotcheck_flag(
            {"spotcheck_id": "x", "verdict": "looks_right"}, bucket="test-bucket",
        )
        assert ok is False
