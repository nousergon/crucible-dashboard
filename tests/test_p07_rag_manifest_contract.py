"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10873,
I10934), D16.

Pinned copy of nousergon-data's ``contracts/rag_manifest.schema.json`` lives at
``tests/contracts/rag_manifest.schema.json`` here (mirrors the
``feature_registry``/``news_article_row`` precedent from crucible-dashboard-PR862,
same P-07 batch) — this repo never imports nousergon-data; the versioned JSON
Schema is the coupling.

Covers:
  1. the pinned schema is itself a valid JSON Schema;
  2. a schema-conformant fixture validates;
  3. a payload missing a required top-level key fails validation;
  4. the REAL consumer reader — ``loaders/s3_loader.py::load_rag_manifest``
     (reads ``rag/manifest/latest.json``, per its own docstring: "the
     presentation layer never queries pgvector directly") — round-trips a
     schema-conformant fixture and returns exactly the fields
     ``views/14_RAG_Inventory.py`` reads off it (``totals``, ``by_source``,
     ``by_ticker_coverage``, ``embedding``, ``ingestion.last_run_ts``,
     ``generated_at``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import jsonschema

CONTRACTS_DIR = Path(__file__).parent / "contracts"

_MOCK_CONFIG = {
    "s3": {"research_bucket": "test-bucket", "trades_bucket": "test-bucket"},
    "cache_ttl": {"signals": 900, "trades": 900, "research": 3600, "backtest": 3600},
    "paths": {"signals": "signals/{date}/signals.json", "research_db": "research.db"},
}


def _import_s3_loader():
    """Force a fresh import of loaders.s3_loader regardless of prior test
    pollution. tests/test_db_loader.py installs a MagicMock under
    ``sys.modules['loaders.s3_loader']`` at collection time (no
    restoration) — the other s3-loader test files clear it via
    ``del sys.modules[...]`` before importing, per the repo-wide
    convention (tests/test_llm_cost_loader.py, tests/test_s3_loader.py)."""
    if "loaders.s3_loader" in sys.modules:
        del sys.modules["loaders.s3_loader"]
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
            from loaders import s3_loader
            return s3_loader


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "rag_manifest.schema.json").read_text())


def _fixture() -> dict:
    return {
        "generated_at": "2026-09-20T05:12:00Z",
        "schema_version": "1",
        "totals": {"documents": 4200, "chunks": 118000, "tickers": 812},
        "by_source": {
            "10-K": {"documents": 900, "tickers": 812, "chunks": 40000},
            "10-Q": {"documents": 2400, "tickers": 800, "chunks": 60000},
            "earnings_transcript": {"documents": 900, "tickers": 700, "chunks": 18000},
        },
        "by_ticker_coverage": {
            "tickers_with_any_doc": 812,
            "p25_docs_per_ticker": 2,
            "p50_docs_per_ticker": 5,
            "p75_docs_per_ticker": 9,
        },
        "embedding": {"model": "voyage-3-lite", "dimension": 512},
        "ingestion": {
            "last_run_ts": "2026-09-20T05:10:00Z",
            "by_source_last_ts": {"10-K": "2026-09-20T05:00:00Z"},
            "by_date_source": [
                {"date": "2026-09-20", "doc_type": "10-K", "documents": 900, "chunks": 40000},
            ],
        },
    }


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


class TestFixtureValidatesAgainstPinnedSchema:
    def test_full_manifest_validates(self):
        jsonschema.validate(instance=_fixture(), schema=_schema())

    def test_missing_required_top_level_key_is_rejected(self):
        bad = _fixture()
        del bad["totals"]
        try:
            jsonschema.validate(instance=bad, schema=_schema())
        except jsonschema.ValidationError:
            pass
        else:
            raise AssertionError("expected ValidationError for missing 'totals'")

    def test_totals_missing_required_field_is_rejected(self):
        bad = _fixture()
        del bad["totals"]["chunks"]
        try:
            jsonschema.validate(instance=bad, schema=_schema())
        except jsonschema.ValidationError:
            pass
        else:
            raise AssertionError("expected ValidationError for missing 'totals.chunks'")


class TestRealConsumerReaderExtractsPinnedFields:
    """loaders.s3_loader.load_rag_manifest is the REAL reader
    views/14_RAG_Inventory.py drives — a schema-conformant fixture must
    round-trip through it and carry every field the view reads off it."""

    def test_schema_conformant_fixture_round_trips_through_the_real_loader(self):
        mod = _import_s3_loader()
        fixture = _fixture()
        jsonschema.validate(instance=fixture, schema=_schema())
        with patch.object(mod, "_fetch_s3_json", return_value=fixture):
            manifest = mod.load_rag_manifest()

        assert manifest == fixture
        totals = manifest["totals"]
        assert totals["documents"] == 4200
        assert totals["chunks"] == 118000
        assert totals["tickers"] == 812

        by_source = manifest["by_source"]
        assert by_source["10-K"]["documents"] == 900

        coverage = manifest["by_ticker_coverage"]
        assert coverage["tickers_with_any_doc"] == 812
        assert coverage["p50_docs_per_ticker"] == 5

        assert manifest["embedding"]["model"] == "voyage-3-lite"
        assert manifest["ingestion"]["last_run_ts"] == "2026-09-20T05:10:00Z"
        assert manifest["generated_at"] == "2026-09-20T05:12:00Z"

    def test_absent_manifest_returns_none(self):
        mod = _import_s3_loader()
        with patch.object(mod, "_fetch_s3_json", return_value=None):
            assert mod.load_rag_manifest() is None
