"""Unit tests for health_checker — data staleness and pipeline health checks."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, datetime, timezone, timedelta

from health_checker import (
    check_all,
    format_report,
    _last_modified_age,
    _find_latest_prefix,
    THRESHOLDS,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_head_object(ages: dict[str, int]):
    """Return a mock S3 head_object that returns LastModified based on key→age mapping."""
    def head_object(Bucket, Key):
        for pattern, age in ages.items():
            if pattern in Key:
                return {
                    "LastModified": datetime.now(timezone.utc) - timedelta(days=age)
                }
        raise Exception("NoSuchKey")
    return head_object


def _mock_list_objects(prefix_dates: dict[str, str]):
    """Return a mock paginator for list_objects_v2 with date-keyed prefixes."""
    class MockPaginator:
        def __init__(self, prefix_dates):
            self._prefix_dates = prefix_dates

        def paginate(self, Bucket, Prefix, MaxKeys=100):
            date_str = self._prefix_dates.get(Prefix)
            if date_str:
                yield {"Contents": [{"Key": f"{Prefix}{date_str}/data.parquet"}]}
            else:
                yield {"Contents": []}

    paginator = MockPaginator(prefix_dates)

    def get_paginator(method):
        return paginator

    return get_paginator


# ═══════════════════════════════════════════════════════════════════════════════
# _last_modified_age
# ═══════════════════════════════════════════════════════════════════════════════


class TestLastModifiedAge:
    def test_returns_age_for_existing_object(self):
        s3 = MagicMock()
        s3.head_object.return_value = {
            "LastModified": datetime.now(timezone.utc) - timedelta(days=3)
        }
        modified, age = _last_modified_age(s3, "bucket", "key")
        assert age == 3
        assert modified is not None

    def test_returns_none_for_missing_object(self):
        s3 = MagicMock()
        s3.head_object.side_effect = Exception("NoSuchKey")
        modified, age = _last_modified_age(s3, "bucket", "key")
        assert modified is None
        assert age is None


# ═══════════════════════════════════════════════════════════════════════════════
# check_all
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckAll:
    @patch("health_checker.boto3")
    def test_all_checks_present(self, mock_boto3):
        """Every THRESHOLDS key should produce a check result."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3

        # Make all head_object calls return a fresh object
        s3.head_object.return_value = {
            "LastModified": datetime.now(timezone.utc) - timedelta(hours=1)
        }
        # Make list_objects work for prefix-based checks
        paginator = MagicMock()
        today = date.today().isoformat()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"prefix/{today}/data.parquet"}]}
        ]
        s3.get_paginator.return_value = paginator

        results = check_all()
        check_names = {r["check"] for r in results}

        # Core data checks
        assert "signals" in check_names
        assert "predictions" in check_names
        assert "features" in check_names
        assert "fundamentals" in check_names
        assert "population" in check_names
        # price_cache_slim check RETIRED (Wave-4): the slim tier is being
        # deleted; ArcticDB-universe freshness is gated upstream in
        # alpha-engine-data's preflight. Guard against accidental reinstate.
        assert "price_cache_slim" not in check_names
        assert "daily_closes" in check_names

        # Module health markers
        assert "health/data_phase1" in check_names
        assert "health/executor" in check_names

    @patch("health_checker.boto3")
    def test_fresh_data_returns_ok(self, mock_boto3):
        """Objects modified within threshold should be 'ok'."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3

        s3.head_object.return_value = {
            "LastModified": datetime.now(timezone.utc) - timedelta(hours=1)
        }
        paginator = MagicMock()
        today = date.today().isoformat()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"prefix/{today}/data.parquet"}]}
        ]
        s3.get_paginator.return_value = paginator

        results = check_all()
        statuses = {r["status"] for r in results}
        assert "ok" in statuses

    @patch("health_checker.boto3")
    def test_stale_data_returns_stale(self, mock_boto3):
        """Objects older than threshold should be 'stale'."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3

        # Make everything look 30 days old
        s3.head_object.return_value = {
            "LastModified": datetime.now(timezone.utc) - timedelta(days=30)
        }
        old_date = (date.today() - timedelta(days=30)).isoformat()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"prefix/{old_date}/data.parquet"}]}
        ]
        s3.get_paginator.return_value = paginator

        results = check_all()
        stale_checks = [r for r in results if r["status"] == "stale"]
        # predictions (2d threshold) and features (2d) should definitely be stale at 30d
        stale_names = {r["check"] for r in stale_checks}
        assert "predictions" in stale_names

    @patch("health_checker.boto3")
    def test_missing_data_returns_missing(self, mock_boto3):
        """Objects that don't exist should be 'missing'."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3

        s3.head_object.side_effect = Exception("NoSuchKey")
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        s3.get_paginator.return_value = paginator

        results = check_all()
        assert all(r["status"] == "missing" for r in results)


# ═══════════════════════════════════════════════════════════════════════════════
# format_report
# ═══════════════════════════════════════════════════════════════════════════════


class TestFormatReport:
    def test_ok_report(self):
        results = [{"check": "signals", "status": "ok", "age_days": 1,
                     "threshold_days": 8, "last_updated": "2026-04-03"}]
        report = format_report(results)
        assert "OK: 1" in report
        assert "Stale: 0" in report

    def test_stale_report_includes_actions(self):
        results = [{"check": "predictions", "status": "stale", "age_days": 5,
                     "threshold_days": 2, "last_updated": "2026-03-29"}]
        report = format_report(results)
        assert "ACTIONS NEEDED" in report
        assert "predictions" in report

    def test_missing_report(self):
        results = [{"check": "features", "status": "missing", "age_days": None,
                     "threshold_days": 2, "last_updated": None}]
        report = format_report(results)
        assert "Missing: 1" in report
