"""Tests for the changelog-quarantine loader (pure mapping logic + the
"Approve & migrate to entries/" write path, config#868)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from loaders import quarantine_loader as ql
from loaders.quarantine_loader import (
    _QUARANTINE_COLUMNS,
    _quarantine_to_row,
    migrate_quarantine_entry,
)

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
    # Quarantine is a lens of the consolidated Incidents tab (console-IA
    # phase 1, config#1990), which the Observability front page hosts.
    incidents_src = (REPO_ROOT / "views" / "Incidents.py").read_text()
    assert "41_Quarantine.py" in incidents_src
    host_src = (REPO_ROOT / "views" / "host_observability.py").read_text()
    assert "Incidents.py" in host_src
    app_src = (REPO_ROOT / "app.py").read_text()
    assert 'page("host_observability.py"' in app_src
    assert (REPO_ROOT / "views" / "41_Quarantine.py").exists()


def test_loader_reads_quarantine_prefix():
    """The loader must read the producer's exact prefix — a drift here
    silently shows an empty (always-healthy-looking) triage page."""
    src = (REPO_ROOT / "loaders" / "quarantine_loader.py").read_text()
    assert "changelog/quarantine" in src


# ── migrate_quarantine_entry ("Approve & migrate to entries/", config#868) ──


def _get_object_response(entry: dict) -> dict:
    body = MagicMock()
    body.read.return_value = json.dumps(entry).encode()
    return {"Body": body}


class TestMigrateQuarantineEntry:
    def test_migrate_success_copies_then_deletes(self):
        entry = _sample_quarantined_entry()
        client = MagicMock()
        client.get_object.return_value = _get_object_response(entry)
        with patch.object(ql, "get_s3_client", return_value=client), patch.object(
            ql, "_research_bucket", return_value="alpha-engine-research"
        ):
            ok, msg = migrate_quarantine_entry("2026-06-27", entry["event_id"])

        assert ok is True
        assert "changelog/entries/2026-06-27/" in msg

        client.get_object.assert_called_once_with(
            Bucket="alpha-engine-research",
            Key=f"changelog/quarantine/2026-06-27/{entry['event_id']}.json",
        )
        put_kwargs = client.put_object.call_args.kwargs
        assert put_kwargs["Bucket"] == "alpha-engine-research"
        assert put_kwargs["Key"] == f"changelog/entries/2026-06-27/{entry['event_id']}.json"
        assert json.loads(put_kwargs["Body"]) == entry

        client.delete_object.assert_called_once_with(
            Bucket="alpha-engine-research",
            Key=f"changelog/quarantine/2026-06-27/{entry['event_id']}.json",
        )

    def test_migrate_read_failure_does_not_write_or_delete(self):
        client = MagicMock()
        client.get_object.side_effect = Exception("NoSuchKey")
        with patch.object(ql, "get_s3_client", return_value=client), patch.object(
            ql, "_research_bucket", return_value="alpha-engine-research"
        ):
            ok, msg = migrate_quarantine_entry("2026-06-27", "evt1")

        assert ok is False
        assert "could not read" in msg
        client.put_object.assert_not_called()
        client.delete_object.assert_not_called()

    def test_migrate_write_failure_leaves_quarantine_copy_in_place(self):
        """A 403 (IAM grant not yet applied live) must surface as a clear
        operator-facing message, not crash the page — and must NOT delete
        the quarantine copy, since the migrate write never landed."""
        entry = _sample_quarantined_entry()
        client = MagicMock()
        client.get_object.return_value = _get_object_response(entry)
        client.put_object.side_effect = Exception(
            "An error occurred (AccessDenied) when calling the PutObject operation"
        )
        with patch.object(ql, "get_s3_client", return_value=client), patch.object(
            ql, "_research_bucket", return_value="alpha-engine-research"
        ):
            ok, msg = migrate_quarantine_entry("2026-06-27", entry["event_id"])

        assert ok is False
        assert "AccessDenied" in msg
        assert "IAM grant" in msg
        client.delete_object.assert_not_called()

    def test_migrate_delete_failure_reports_dual_existence(self):
        entry = _sample_quarantined_entry()
        client = MagicMock()
        client.get_object.return_value = _get_object_response(entry)
        client.delete_object.side_effect = Exception("AccessDenied")
        with patch.object(ql, "get_s3_client", return_value=client), patch.object(
            ql, "_research_bucket", return_value="alpha-engine-research"
        ):
            ok, msg = migrate_quarantine_entry("2026-06-27", entry["event_id"])

        assert ok is False
        assert "migrated to" in msg
        assert "failed to delete" in msg
        client.put_object.assert_called_once()

    def test_migrate_unparseable_entry_reports_json_error(self):
        client = MagicMock()
        body = MagicMock()
        body.read.return_value = b"not json"
        client.get_object.return_value = {"Body": body}
        with patch.object(ql, "get_s3_client", return_value=client), patch.object(
            ql, "_research_bucket", return_value="alpha-engine-research"
        ):
            ok, msg = migrate_quarantine_entry("2026-06-27", "evt1")

        assert ok is False
        assert "not valid JSON" in msg
        client.put_object.assert_not_called()
        client.delete_object.assert_not_called()
