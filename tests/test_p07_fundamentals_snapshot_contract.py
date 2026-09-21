"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10873,
I10934), D10.

Pinned copy of nousergon-data's ``contracts/fundamentals_snapshot.schema.json``
lives at ``tests/contracts/fundamentals_snapshot.schema.json`` here (mirrors the
``feature_registry``/``news_article_row`` precedent from crucible-dashboard-PR862,
same P-07 batch) — this repo never imports nousergon-data; the versioned JSON
Schema is the coupling.

Covers:
  1. the pinned schema is itself a valid JSON Schema;
  2. a schema-conformant fixture (both a real-looking row and the NEUTRAL
     sentinel shape nousergon-data's ``collectors/fundamentals.py::collect``
     writes for tickers with no usable Finnhub data) validates;
  3. a row missing a required field, or carrying an unexpected extra field,
     fails validation (the schema is ``additionalProperties: false`` per row);
  4. the REAL consumer reader — ``health_checker.py``'s ``fundamentals``
     freshness check (``_find_latest_prefix`` over ``archive/fundamentals/``,
     ``THRESHOLDS['fundamentals']=100``) — correctly reads the age of a
     schema-conformant snapshot object keyed at the schema's own
     ``x-key-pattern`` (``archive/fundamentals/{date}.json``).

``health_checker.py``'s fundamentals check only reads the OBJECT KEY's date
(``_find_latest_prefix`` lists keys, it never GETs the body) — it does not
parse the snapshot's JSON content. The schema instead pins the shape for the
one dashboard consumer that DOES read content today
(crucible-research's v1 ``agents/sector_teams/quant_tools.py``, retiring) and
for any future dashboard reader of ``archive/fundamentals/`` content. Test 4
below exercises exactly what health_checker.py actually reads: presence +
freshness of the schema's own key pattern, not a fabricated content read.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from health_checker import THRESHOLDS, _find_latest_prefix

CONTRACTS_DIR = Path(__file__).parent / "contracts"

NEUTRAL_ROW = {
    "pe_ratio": 0.0,
    "pb_ratio": 0.0,
    "debt_to_equity": 0.0,
    "revenue_growth_yoy": 0.0,
    "fcf_yield": 0.0,
    "gross_margin": 0.0,
    "roe": 0.0,
    "current_ratio": 0.0,
    "revenue_growth_3y": 0.0,
    "eps_growth_3y": 0.0,
    "payout_ratio": 0.0,
    "dividend_yield": 0.0,
    "capex_growth_5y": 0.0,
    "market_cap_raw": 0.0,
}


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "fundamentals_snapshot.schema.json").read_text())


def _real_row(**overrides) -> dict:
    row = dict(NEUTRAL_ROW)
    row.update(pe_ratio=28.4, market_cap_raw=3_000_000_000_000.0)
    row.update(overrides)
    return row


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


class TestFixtureValidatesAgainstPinnedSchema:
    def test_snapshot_with_real_row_validates(self):
        jsonschema.validate(instance={"AAPL": _real_row()}, schema=_schema())

    def test_neutral_sentinel_row_validates(self):
        jsonschema.validate(instance={"XYZ": dict(NEUTRAL_ROW)}, schema=_schema())

    def test_empty_snapshot_validates(self):
        jsonschema.validate(instance={}, schema=_schema())

    def test_row_missing_required_field_is_rejected(self):
        bad = _real_row()
        del bad["pe_ratio"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance={"AAPL": bad}, schema=_schema())

    def test_row_with_extra_field_is_rejected(self):
        bad = _real_row(unexpected_field=1.0)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance={"AAPL": bad}, schema=_schema())


class TestRealConsumerReaderReadsThePinnedKeyPattern:
    """health_checker.py's fundamentals freshness check
    (`_find_latest_prefix` over `archive/fundamentals/`) is the ACTUAL
    consumer — exercised here against an object keyed per the schema's own
    `x-key-pattern`, with a schema-conformant body (proving the two artifacts
    — key and content — are produced together by the real writer, even
    though this particular reader only consumes the key)."""

    def test_fresh_snapshot_key_reads_as_ok(self):
        snapshot_date = date.today() - timedelta(days=5)
        key = f"archive/fundamentals/{snapshot_date.isoformat()}.json"
        body = json.dumps({"AAPL": _real_row()})
        jsonschema.validate(instance=json.loads(body), schema=_schema())

        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": [{"Key": key}]}]
        mock_s3.get_paginator.return_value = mock_paginator

        latest_date, age = _find_latest_prefix(mock_s3, "test-bucket", "archive/fundamentals/")
        assert latest_date == snapshot_date.isoformat()
        assert age == 5
        assert age <= THRESHOLDS["fundamentals"]

    def test_stale_snapshot_key_exceeds_threshold(self):
        snapshot_date = date.today() - timedelta(days=THRESHOLDS["fundamentals"] + 1)
        key = f"archive/fundamentals/{snapshot_date.isoformat()}.json"

        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": [{"Key": key}]}]
        mock_s3.get_paginator.return_value = mock_paginator

        _, age = _find_latest_prefix(mock_s3, "test-bucket", "archive/fundamentals/")
        assert age > THRESHOLDS["fundamentals"]
