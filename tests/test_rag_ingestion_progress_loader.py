"""Unit tests for the RAGIngestion inner-step progress loader
(loaders.fleet_status_loader.rag_ingestion_progress, config-I2966).

Mocks ``download_s3_json`` directly — no live S3/network. Covers the
happy path, absence (pre-write / aged out), and malformed-artifact
degradation (missing required keys, wrong types) — all of which must
degrade to ``None`` gracefully since this is enrichment, never authority,
for the strip's RUNNING/done/pending state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import loaders.fleet_status_loader as fsl  # noqa: E402
from fleet_status import RagIngestionProgress  # noqa: E402


def _clear_cache():
    # _rag_ingestion_progress_raw is st.cache_data-wrapped in production;
    # under the test conftest's streamlit mock, cache_data is a passthrough
    # (no .clear needed), but guard anyway for safety/symmetry with the
    # rest of this loader's cache-clear patterns.
    getattr(fsl._rag_ingestion_progress_raw, "clear", lambda: None)()


class TestRagIngestionProgress:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setattr(
            fsl, "download_s3_json",
            lambda bucket, key: {
                "step": 5, "of": 10, "label": "news",
                "started_at": "2026-07-25T09:00:00Z",
                "updated_at": "2026-07-25T11:55:00Z",
            },
        )
        _clear_cache()
        result = fsl.rag_ingestion_progress("2026-07-25")
        assert result == RagIngestionProgress(
            step=5, of=10, label="news",
            started_at="2026-07-25T09:00:00Z",
            updated_at="2026-07-25T11:55:00Z",
        )

    def test_uses_correct_key_template(self, monkeypatch):
        seen = {}

        def _fake(bucket, key):
            seen["bucket"] = bucket
            seen["key"] = key
            return None

        monkeypatch.setattr(fsl, "download_s3_json", _fake)
        _clear_cache()
        fsl.rag_ingestion_progress("2026-07-25")
        assert seen["key"] == "health/rag_ingestion_progress/2026-07-25.json"

    def test_absent_artifact_returns_none(self, monkeypatch):
        monkeypatch.setattr(fsl, "download_s3_json", lambda bucket, key: None)
        _clear_cache()
        assert fsl.rag_ingestion_progress("2026-07-25") is None

    def test_non_dict_artifact_returns_none(self, monkeypatch):
        monkeypatch.setattr(fsl, "download_s3_json", lambda bucket, key: [1, 2, 3])
        _clear_cache()
        assert fsl.rag_ingestion_progress("2026-07-25") is None

    def test_missing_required_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            fsl, "download_s3_json",
            lambda bucket, key: {"step": 5, "label": "news"},  # missing "of"
        )
        _clear_cache()
        assert fsl.rag_ingestion_progress("2026-07-25") is None

    def test_malformed_step_type_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            fsl, "download_s3_json",
            lambda bucket, key: {"step": "not-a-number", "of": 10, "label": "news"},
        )
        _clear_cache()
        assert fsl.rag_ingestion_progress("2026-07-25") is None

    def test_optional_timestamps_absent_still_parses(self, monkeypatch):
        monkeypatch.setattr(
            fsl, "download_s3_json",
            lambda bucket, key: {"step": 1, "of": 10, "label": "preflight"},
        )
        _clear_cache()
        result = fsl.rag_ingestion_progress("2026-07-25")
        assert result is not None
        assert result.started_at is None
        assert result.updated_at is None
