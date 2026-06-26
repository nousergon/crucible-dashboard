"""Tests for the changelog event-lake loader (pure mapping logic)."""

from __future__ import annotations

from loaders.changelog_loader import _COLUMNS, _entry_to_row


def _sample_entry() -> dict:
    return {
        "schema_version": "1.0.0",
        "event_id": "20260626T120000Z_research_abc1234",
        "ts_utc": "2026-06-26T12:00:00Z",
        "event_type": "incident",
        "severity": "high",
        "subsystem": "research",
        "root_cause_category": "data_quality",
        "summary": "research-lambda: KeyError on signals.json",
        "actor": "research",
        "source": "flow-doctor",
        "flow_doctor": {
            "error_signature": "KeyError:signals",
            "dedup_count": 3,
            "cascade_source": None,
            "diagnosis": {"category": "data_quality", "confidence": 0.8},
        },
    }


def test_entry_to_row_maps_top_level_and_flow_doctor_fields():
    row = _entry_to_row(_sample_entry())
    assert row["ts_utc"] == "2026-06-26T12:00:00Z"
    assert row["severity"] == "high"
    assert row["subsystem"] == "research"
    assert row["source"] == "flow-doctor"
    assert row["root_cause_category"] == "data_quality"
    # nested flow_doctor provenance is lifted to top-level columns
    assert row["error_signature"] == "KeyError:signals"
    assert row["dedup_count"] == 3
    assert row["event_id"].endswith("abc1234")


def test_entry_to_row_tolerates_missing_flow_doctor_block():
    """CloudWatch-mirror / SNS-mirror entries have no flow_doctor block."""
    entry = {
        "ts_utc": "2026-06-26T01:00:00Z",
        "event_type": "incident",
        "severity": "high",
        "subsystem": "infrastructure",
        "summary": "alpha-engine-pipeline-watchdog timed out",
        "source": "changelog-cloudwatch-mirror",
    }
    row = _entry_to_row(entry)
    assert row["error_signature"] is None
    assert row["dedup_count"] is None
    assert row["subsystem"] == "infrastructure"


def test_row_keys_match_column_contract():
    row = _entry_to_row(_sample_entry())
    assert set(row.keys()) == set(_COLUMNS)
