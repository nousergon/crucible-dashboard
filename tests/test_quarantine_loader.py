"""Tests for the changelog-quarantine loader (pure mapping logic)."""

from __future__ import annotations

from pathlib import Path

from loaders.quarantine_loader import _QUARANTINE_COLUMNS, _quarantine_to_row

REPO_ROOT = Path(__file__).parent.parent


def _sample_quarantined_entry() -> dict:
    """A schema-1.0.0 changelog entry rejected for a bad ``subsystem``."""
    return {
        "schema_version": "1.0.0",
        "event_id": "20260627T120000Z_deploy_abc1234",
        "ts_utc": "2026-06-27T12:00:00Z",
        "event_type": "change",
        "severity": "low",
        "subsystem": "dashbard",  # typo — not in the allowed set
        "root_cause_category": "configuration",
        "summary": "deploy: crucible-dashboard main",
        "actor": "cipher813",
        "source": "append-changelog",
        "validation_errors": [
            "vocab field 'subsystem'='dashbard' not in allowed set",
        ],
    }


def test_quarantine_to_row_maps_fields_and_joins_errors():
    row = _quarantine_to_row(_sample_quarantined_entry())
    assert row["ts_utc"] == "2026-06-27T12:00:00Z"
    assert row["subsystem"] == "dashbard"
    assert row["source"] == "append-changelog"
    assert row["event_id"].endswith("abc1234")
    # validation_errors list is joined to a readable string
    assert row["validation_errors"] == (
        "vocab field 'subsystem'='dashbard' not in allowed set"
    )


def test_quarantine_to_row_joins_multiple_errors():
    entry = _sample_quarantined_entry()
    entry["validation_errors"] = [
        "vocab field 'subsystem'='dashbard' not in allowed set",
        "vocab field 'severity'='urgent' not in allowed set",
    ]
    row = _quarantine_to_row(entry)
    assert (
        row["validation_errors"]
        == "vocab field 'subsystem'='dashbard' not in allowed set; "
        "vocab field 'severity'='urgent' not in allowed set"
    )


def test_quarantine_to_row_tolerates_missing_validation_errors():
    """An object that reached quarantine without the field must not raise."""
    entry = _sample_quarantined_entry()
    del entry["validation_errors"]
    row = _quarantine_to_row(entry)
    assert row["validation_errors"] == ""
    assert row["subsystem"] == "dashbard"


def test_quarantine_to_row_tolerates_non_list_validation_errors():
    entry = _sample_quarantined_entry()
    entry["validation_errors"] = "single string reason"
    row = _quarantine_to_row(entry)
    assert row["validation_errors"] == "single string reason"


def test_row_keys_match_column_contract():
    row = _quarantine_to_row(_sample_quarantined_entry())
    assert set(row.keys()) == set(_QUARANTINE_COLUMNS)


def test_page_registered_in_nav():
    app_src = (REPO_ROOT / "app.py").read_text()
    assert "41_Quarantine.py" in app_src
    assert (REPO_ROOT / "views" / "41_Quarantine.py").exists()


def test_loader_reads_quarantine_prefix():
    """The loader must read the producer's exact prefix — a drift here
    silently shows an empty (always-healthy-looking) triage page."""
    src = (REPO_ROOT / "loaders" / "quarantine_loader.py").read_text()
    assert "changelog/quarantine" in src
