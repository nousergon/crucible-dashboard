"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10873,
I10934), D10.

CORRECTED 2026-09-21: a first version of this test pinned a byte-copy of
nousergon-data's ``contracts/fundamentals_snapshot.schema.json`` (the object
BODY shape). That was half a contract: ``health_checker.py``'s ``fundamentals``
freshness check (the only D10 consumer in this repo — see
``registry.d/units/D10-fundamentals.yaml`` `consumers`) is
``_find_latest_prefix`` over ``archive/fundamentals/``, and that function
never calls ``get_object`` / ``_fetch_s3_json`` for this prefix — it reads
only the OBJECT KEY, never the body. A body-schema pin there cannot fail this
consumer no matter how the body drifts; the thing that CAN break it is the
S3 key template (path + date-format), which was left unpinned.

This version instead pins ``tests/contracts/fundamentals_snapshot_s3_key_template.json``
— a byte-copy of the key template nousergon-data's
``registry.d/units/D10-fundamentals.yaml`` `writes` field declares
(``archive/fundamentals/{date}.json``, ISO date) — and exercises the REAL
consumer (``health_checker.py::_find_latest_prefix``) against keys built
from that pinned template.

(A genuine BODY reader of this artifact does exist — crucible-research's
``agents/sector_teams/quant_tools.py::read_fundamentals_from_s3`` GETs and
``json.loads``s the object — but that is a different repo, already listed
in D10's `consumers`, and out of scope for a crucible-dashboard PR; tracked
separately.)

Covers:
  1. the pinned key-template fixture is well-formed and matches the literal
     string this repo depends on;
  2. the REAL consumer reader (``_find_latest_prefix``) correctly reads the
     age of a key built from the pinned template;
  3. a key one day past the ``fundamentals`` threshold is correctly read as
     over threshold;
  4. a key whose date segment does NOT match the pinned template's date
     format (the thing that would actually break this consumer on producer
     drift) is NOT picked up by the reader — proving the pin is load-bearing.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from health_checker import THRESHOLDS, _find_latest_prefix

CONTRACTS_DIR = Path(__file__).parent / "contracts"


def _pin() -> dict:
    return json.loads((CONTRACTS_DIR / "fundamentals_snapshot_s3_key_template.json").read_text())


def _key_for(d: date) -> str:
    """Build an object key from the pinned template — the same
    `{date}` substitution `collectors/fundamentals.py::collect` performs
    (`key = f"archive/fundamentals/{run_date}.json"`)."""
    template = _pin()["object_key_template"]
    return template.replace("{date}", d.isoformat())


def _mock_s3_with_key(key: str):
    mock_s3 = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"Contents": [{"Key": key}]}]
    mock_s3.get_paginator.return_value = mock_paginator
    return mock_s3


def test_pinned_key_template_matches_the_literal_this_repo_depends_on():
    pin = _pin()
    assert pin["object_key_template"] == "archive/fundamentals/{date}.json"
    assert pin["date_format"] == "YYYY-MM-DD"


class TestRealConsumerReaderReadsThePinnedKeyTemplate:
    """health_checker.py's fundamentals freshness check
    (`_find_latest_prefix` over `archive/fundamentals/`) is the ACTUAL
    consumer, and it only ever reads the object KEY — never the body
    (confirmed: no `get_object`/`_fetch_s3_json` call for this prefix
    anywhere in health_checker.py). These tests build keys from the pinned
    template and drive the real reader against them."""

    def test_fresh_snapshot_key_reads_as_ok(self):
        snapshot_date = date.today() - timedelta(days=5)
        key = _key_for(snapshot_date)
        mock_s3 = _mock_s3_with_key(key)

        latest_date, age = _find_latest_prefix(mock_s3, "test-bucket", "archive/fundamentals/")
        assert latest_date == snapshot_date.isoformat()
        assert age == 5
        assert age <= THRESHOLDS["fundamentals"]

    def test_stale_snapshot_key_exceeds_threshold(self):
        snapshot_date = date.today() - timedelta(days=THRESHOLDS["fundamentals"] + 1)
        key = _key_for(snapshot_date)
        mock_s3 = _mock_s3_with_key(key)

        _, age = _find_latest_prefix(mock_s3, "test-bucket", "archive/fundamentals/")
        assert age > THRESHOLDS["fundamentals"]

    def test_a_key_whose_date_segment_does_not_match_the_pinned_format_is_not_picked_up(self):
        """The load-bearing proof: if the producer drifted the date format
        (e.g. to a timestamp, or dropped the leading zero convention),
        `_find_latest_prefix`'s ISO-date parse would stop matching and the
        check would read `missing` instead of `stale`/`ok` — exactly the
        failure mode this pin exists to catch."""
        template = _pin()["object_key_template"]
        assert re.fullmatch(r"archive/fundamentals/\{date\}\.json", template)
        bad_key = "archive/fundamentals/20260916.json"  # no dashes -> not ISO YYYY-MM-DD
        mock_s3 = _mock_s3_with_key(bad_key)

        latest_date, age = _find_latest_prefix(mock_s3, "test-bucket", "archive/fundamentals/")
        assert latest_date is None
        assert age is None
