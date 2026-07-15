"""Unit tests for health_checker — data staleness and pipeline health checks."""
from unittest.mock import patch, MagicMock
from datetime import date, datetime, timezone, timedelta

from health_checker import (
    check_all,
    format_report,
    _last_modified_age,
    _find_latest_prefix,
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
        assert "health/data" in check_names
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


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: 2026-05-24 health-checker false-positives audit
# ═══════════════════════════════════════════════════════════════════════════════


class TestFilenameKeyedDateParsing:
    """``_find_latest_prefix`` must recognize both directory-keyed
    (``signals/2026-04-03/signals.json``) and filename-keyed
    (``archive/fundamentals/2026-05-24.json``) date conventions.

    Surfaced by the 2026-05-24 health-check email reporting ``fundamentals:
    missing`` even though DataPhase1 had just written
    ``archive/fundamentals/2026-05-24.json``. The legacy
    ``split('/')`` + ``len(part) == 10`` rule didn't strip the ``.json``
    extension so filename-keyed entries never matched.
    """

    def test_filename_keyed_iso_date_with_json_extension(self):
        """Filename-keyed S3 entries (date.json) must be detected."""
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"Contents": [
                {"Key": "archive/fundamentals/2026-05-22.json"},
                {"Key": "archive/fundamentals/2026-05-24.json"},
                {"Key": "archive/fundamentals/2026-05-23.json"},
            ]},
        ])
        s3.get_paginator.return_value = paginator

        latest, age = _find_latest_prefix(s3, "test-bucket", "archive/fundamentals/")
        assert latest == "2026-05-24"
        assert age == (date.today() - date(2026, 5, 24)).days

    def test_directory_keyed_iso_date_still_works(self):
        """Backwards-compat: the directory-keyed shape used by signals/
        and population/ must continue to resolve."""
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"Contents": [
                {"Key": "signals/2026-05-20/signals.json"},
                {"Key": "signals/2026-05-24/signals.json"},
            ]},
        ])
        s3.get_paginator.return_value = paginator

        latest, age = _find_latest_prefix(s3, "test-bucket", "signals/")
        assert latest == "2026-05-24"

    def test_filename_with_non_iso_extension_ignored(self):
        """Non-ISO filenames are correctly skipped (defensive)."""
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"Contents": [
                {"Key": "archive/fundamentals/backup.json"},  # non-date filename
                {"Key": "archive/fundamentals/2026-05-24.json"},
            ]},
        ])
        s3.get_paginator.return_value = paginator

        latest, _ = _find_latest_prefix(s3, "test-bucket", "archive/fundamentals/")
        assert latest == "2026-05-24"


class TestDailyClosesLookbackWindow:
    """The ``daily_closes`` check must walk back across multiple days,
    not just today + yesterday. Saturday/Sunday runs need to find
    Friday's parquet (Fri close = 1-3 calendar days back depending on
    runtime). Surfaced 2026-05-24: Sunday redrive checking
    today(5/24)+yesterday(5/23) found no parquet → false ``missing``
    even though Friday(5/22)'s parquet was 0 trading days behind."""

    def test_finds_parquet_two_days_back(self):
        """Sunday redrive: today + yesterday absent, Friday(2d back) present."""
        from health_checker import check_all
        s3 = MagicMock()
        # Find the Friday parquet 2 days back; everything else returns NoSuchKey
        target_friday = (date.today() - timedelta(days=2)).isoformat()

        def head_object(Bucket, Key):
            if Key == f"staging/daily_closes/{target_friday}.parquet":
                return {"LastModified": datetime.now(timezone.utc) - timedelta(days=2)}
            raise Exception("NoSuchKey")

        s3.head_object.side_effect = head_object
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            results = check_all("test-bucket")
        dc = next(r for r in results if r["check"] == "daily_closes")
        # 2 calendar days back is at-threshold (default 2) → ok
        assert dc["status"] == "ok", (
            f"Daily closes 2-day-back lookback failed: {dc}"
        )
        assert dc["age_days"] == 2


class TestPerModuleHealthCandidates:
    """The predictor module writes its health under filename-specific
    suffixes (``predictor_inference.json``, ``predictor_training.json``,
    ``predictor_health_check.json``) and never a unified ``predictor.json``.
    The checker must accept any of the candidate filenames for that
    module and use the most-recently-modified one.

    Surfaced 2026-05-24: looking for ``health/predictor.json`` returned
    'missing' even though three predictor health surfaces were fresh."""

    def test_predictor_picks_most_recent_candidate(self):
        from health_checker import check_all
        s3 = MagicMock()

        # Only predictor_training.json is fresh; predictor.json missing
        def head_object(Bucket, Key):
            if Key == "health/predictor_training.json":
                return {"LastModified": datetime.now(timezone.utc) - timedelta(hours=2)}
            if Key == "health/predictor_inference.json":
                return {"LastModified": datetime.now(timezone.utc) - timedelta(days=1)}
            if Key == "health/predictor_health_check.json":
                # older — should NOT win
                return {"LastModified": datetime.now(timezone.utc) - timedelta(days=3)}
            if Key == "health/daily_data.json":
                return {"LastModified": datetime.now(timezone.utc) - timedelta(hours=1)}
            if Key == "health/executor.json":
                return {"LastModified": datetime.now(timezone.utc) - timedelta(days=1)}
            raise Exception("NoSuchKey")

        s3.head_object.side_effect = head_object
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            results = check_all("test-bucket")

        predictor = next(r for r in results if r["check"] == "health/predictor")
        # Picks predictor_training.json (most recent of the three)
        assert predictor["status"] == "ok"
        assert predictor.get("source_key") == "predictor_training.json"

    def test_predictor_missing_when_no_candidate_exists(self):
        from health_checker import check_all
        s3 = MagicMock()
        s3.head_object.side_effect = Exception("NoSuchKey")
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            results = check_all("test-bucket")
        predictor = next(r for r in results if r["check"] == "health/predictor")
        assert predictor["status"] == "missing"
