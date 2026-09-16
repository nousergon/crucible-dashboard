"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10870), D36.

Pinned copy of nousergon-data's ``contracts/news_article_row.schema.json`` lives at
``tests/contracts/news_article_row.schema.json`` here (mirrors the metron
``tests/test_p07_crypto_holdings_contract.py`` precedent from the same P-07 batch,
nousergon-data-PR1747) — this repo never imports nousergon-data; the versioned
JSON Schema is the coupling.

Covers:
  1. the pinned schema is itself a valid JSON Schema;
  2. a schema-conformant row, round-tripped through parquet exactly like
     ``tests/test_news_articles_loader.py::_articles_parquet_bytes`` already does,
     validates and feeds through the REAL consumer reader
     (``loaders/s3_loader.py::load_news_articles``) — the field set
     ``views/Daily_News.py`` actually reads (tickers_json, sources_json,
     published_at, lm_sentiment, event_count) is exercised, not just declared;
  3. a row missing a required field fails schema validation.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import jsonschema
import pandas as pd
import pytest

CONTRACTS_DIR = Path(__file__).parent / "contracts"


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "news_article_row.schema.json").read_text())


def _import_s3_loader():
    if "loaders.s3_loader" in sys.modules:
        del sys.modules["loaders.s3_loader"]
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value={
            "s3": {"research_bucket": "test-bucket", "trades_bucket": "test-bucket"},
            "cache_ttl": {"signals": 900, "trades": 900, "research": 3600, "backtest": 3600},
            "paths": {"signals": "signals/{date}/signals.json", "research_db": "research.db"},
        }):
            from loaders import s3_loader
            return s3_loader


def _row(**overrides) -> dict:
    row = {
        "article_fingerprint": "fp1",
        "aggregate_date": "2026-09-15",
        "schema_version": 1,
        "title": "Big news",
        "url": "https://x/1",
        "tickers_json": '["AAPL", "MSFT"]',
        "n_tickers": 2,
        "primary_source": "polygon",
        "sources_json": '["polygon", "yahoo"]',
        "n_sources": 2,
        "trust_weight_max": 1.0,
        "published_at": "2026-09-15T12:00:00Z",
        "body_excerpt": "lead paragraph",
        "authors_json": "[]",
        "tags_json": "[]",
        "lm_sentiment": 0.3,
        "lm_positive_words": 5,
        "lm_negative_words": 1,
        "lm_uncertainty_words": 0,
        "event_count": 1,
        "event_severity_max": 0.5,
        "event_categories": "guidance",
        "top_event_description": "raised guidance",
    }
    row.update(overrides)
    return row


def _parquet_bytes(rows: list[dict]) -> bytes:
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


class TestFixtureValidatesAgainstPinnedSchema:
    def test_full_row_validates(self):
        jsonschema.validate(instance=_row(), schema=_schema())

    def test_row_missing_required_field_is_rejected(self):
        bad = _row()
        del bad["lm_sentiment"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=_schema())


class TestRealConsumerReaderExtractsPinnedFields:
    """loaders.s3_loader.load_news_articles is the REAL reader
    views/Daily_News.py drives; a schema-conformant row round-tripped through
    parquet must still validate and carry every field the view reads."""

    def test_schema_conformant_row_round_trips_through_the_real_loader(self):
        mod = _import_s3_loader()
        row = _row()
        jsonschema.validate(instance=row, schema=_schema())
        parquet = _parquet_bytes([row])
        with patch.object(mod, "_s3_get_object", return_value=parquet):
            df = mod.load_news_articles("data/news_articles_daily/2609150905_articles.parquet")
        assert len(df) == 1
        loaded = df.iloc[0]
        # The exact fields views/Daily_News.py reads off every row.
        assert json.loads(loaded["tickers_json"]) == ["AAPL", "MSFT"]
        assert json.loads(loaded["sources_json"]) == ["polygon", "yahoo"]
        assert loaded["published_at"] == "2026-09-15T12:00:00Z"
        assert loaded["lm_sentiment"] == pytest.approx(0.3)
        assert int(loaded["event_count"]) == 1

    def test_multiple_rows_round_trip(self):
        mod = _import_s3_loader()
        rows = [_row(article_fingerprint="a", title="A"), _row(article_fingerprint="b", title="B")]
        for r in rows:
            jsonschema.validate(instance=r, schema=_schema())
        parquet = _parquet_bytes(rows)
        with patch.object(mod, "_s3_get_object", return_value=parquet):
            df = mod.load_news_articles("key")
        assert len(df) == 2
