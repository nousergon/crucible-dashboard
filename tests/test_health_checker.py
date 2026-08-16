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
        assert "universe_membership" in check_names
        # price_cache_slim check RETIRED (Wave-4): the slim tier is being
        # deleted; ArcticDB-universe freshness is gated upstream in
        # alpha-engine-data's preflight. Guard against accidental reinstate.
        assert "price_cache_slim" not in check_names
        # population check RETIRED (alpha-engine-config-I6053): its sole
        # producer (the multi-agent research graph's archive_writer) was
        # removed from the weekly SF 2026-07-14, and its consumer was
        # repointed to universe_membership 2026-07-27. Guard against
        # accidental reinstate — a freshness check on a dead producer can
        # only ever report stale.
        assert "population" not in check_names
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


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: alpha-engine-config-I6053 — the two stale entries that would have
# FAILED the 2026-08-15 weekly run outright
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetiredPopulationCheck:
    """`population/latest.json` has had no producer since 2026-07-10.

    Its sole writer was `save_population()` in the multi-agent research
    graph's `archive_writer` node, removed from the weekly SF on 2026-07-14
    (nousergon-data#814, config-I2515). Its consumer — the predictor's daily
    scoring universe — was repointed to `universe_membership` on 2026-07-27
    (config-I4818). The check therefore reported `stale` on every run from
    ~2026-07-19 onward and could never report anything else.

    Because config-I6891 (2026-08-12) makes a degraded weekly run terminate
    in a `Fail` state, this permanently-stale entry stopped being cosmetic
    and became a guaranteed weekly-pipeline failure.
    """

    def test_population_key_is_never_probed(self):
        """Stronger than checking the result name: assert no S3 call is made
        against the dead key at all, so a reinstate cannot sneak in behind a
        renamed check."""
        from health_checker import check_all
        s3 = MagicMock()
        probed: list[str] = []

        def head_object(Bucket, Key):
            probed.append(Key)
            return {"LastModified": datetime.now(timezone.utc) - timedelta(hours=1)}

        s3.head_object.side_effect = head_object
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            check_all("test-bucket")

        assert not any(k.startswith("population/") for k in probed), (
            f"health_checker still probes the retired population/ prefix: "
            f"{[k for k in probed if k.startswith('population/')]}"
        )

    def test_universe_membership_is_probed_in_its_place(self):
        from health_checker import check_all
        s3 = MagicMock()
        probed: list[str] = []

        def head_object(Bucket, Key):
            probed.append(Key)
            return {"LastModified": datetime.now(timezone.utc) - timedelta(hours=1)}

        s3.head_object.side_effect = head_object
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            results = check_all("test-bucket")

        assert "universe_membership/latest.json" in probed
        um = next(r for r in results if r["check"] == "universe_membership")
        assert um["status"] == "ok"
        assert um["threshold_days"] == 8

    def test_universe_membership_still_goes_stale(self):
        """The replacement must be a live detector, not a decoration: a dead
        successor producer has to surface exactly as the dead predecessor
        should have."""
        from health_checker import check_all
        s3 = MagicMock()

        def head_object(Bucket, Key):
            if Key == "universe_membership/latest.json":
                return {"LastModified": datetime.now(timezone.utc) - timedelta(days=20)}
            return {"LastModified": datetime.now(timezone.utc) - timedelta(hours=1)}

        s3.head_object.side_effect = head_object
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            results = check_all("test-bucket")

        um = next(r for r in results if r["check"] == "universe_membership")
        assert um["status"] == "stale"


class TestCadenceDerivedHealthThresholds:
    """Module health markers are judged against their producer's declared
    cadence plus one period of slack (ARCHITECTURE §128), not a flat 2 days.

    Measured live 2026-08-13: `health/backtester.json` was last written
    2026-08-08 by the previous weekly run, which SUCCEEDED — 5 days, reported
    stale against the flat 2-day budget. `health/research.json` has the same
    weekly cadence. A budget a healthy weekly producer cannot meet on four
    days of every week is a broken detector, not a tight one.
    """

    def _run(self, ages_by_key: dict[str, int]):
        from health_checker import check_all
        s3 = MagicMock()

        def head_object(Bucket, Key):
            if Key in ages_by_key:
                return {
                    "LastModified": datetime.now(timezone.utc)
                    - timedelta(days=ages_by_key[Key])
                }
            raise Exception("NoSuchKey")

        s3.head_object.side_effect = head_object
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        s3.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=s3):
            return check_all("test-bucket")

    def test_weekly_backtester_marker_ok_at_five_days(self):
        results = self._run({"health/backtester.json": 5})
        bt = next(r for r in results if r["check"] == "health/backtester")
        assert bt["threshold_days"] == 8
        assert bt["status"] == "ok"

    def test_weekly_research_marker_ok_at_five_days(self):
        results = self._run({"health/research.json": 5})
        rs = next(r for r in results if r["check"] == "health/research")
        assert rs["threshold_days"] == 8
        assert rs["status"] == "ok"

    def test_weekly_markers_still_catch_a_missed_cycle(self):
        """8 days catches a weekly producer that misses one whole cycle —
        which is the state health/research.json has actually been in since
        2026-07-21. The threshold is a budget, not an accommodation."""
        results = self._run(
            {"health/research.json": 23, "health/backtester.json": 12},
        )
        rs = next(r for r in results if r["check"] == "health/research")
        bt = next(r for r in results if r["check"] == "health/backtester")
        assert rs["status"] == "stale"
        assert bt["status"] == "stale"

    def test_daily_markers_keep_the_two_day_budget(self):
        """The daily producers must NOT inherit the weekly slack."""
        results = self._run(
            {
                "health/daily_data.json": 3,
                "health/executor.json": 3,
                "health/predictor_inference.json": 3,
            },
        )
        for name in ("health/data", "health/executor", "health/predictor"):
            r = next(x for x in results if x["check"] == name)
            assert r["threshold_days"] == 2, name
            assert r["status"] == "stale", name

    def test_unknown_module_falls_back_to_the_daily_budget(self):
        """A module added to HEALTH_CHECK_CANDIDATES without a declared
        cadence must read stale and prompt an explicit entry, never inherit
        the most forgiving budget in the table."""
        import health_checker

        candidates = dict(health_checker.HEALTH_CHECK_CANDIDATES)
        candidates["newmodule"] = ("newmodule.json",)
        with patch.object(health_checker, "HEALTH_CHECK_CANDIDATES", candidates):
            results = self._run({"health/newmodule.json": 5})

        nm = next(r for r in results if r["check"] == "health/newmodule")
        assert nm["threshold_days"] == health_checker.DEFAULT_HEALTH_THRESHOLD_DAYS == 2
        assert nm["status"] == "stale"


class TestFeaturesFreshnessProbe:
    """config-I7434.

    The features probe used to HeadObject `features/{today}/technical.parquet`
    then `features/{yesterday}/...` and report `missing` when neither existed —
    a two-day lookback under a threshold that PERMITS two days. An artifact
    sitting exactly at the boundary its own threshold accepts was reported as
    never having existed.

    Measured 2026-08-16 on weekly-SF execution `watch-rerun-2026-08-16-3`:

        features   age=N/A  threshold=2d  last=never

    while `s3://alpha-engine-research/features/2026-08-14/` existed and the
    SAME check had reported `age=1d last=2026-08-14 20:32 UTC` seventeen hours
    earlier. Nothing about the data changed — only which side of a hard-coded
    two-date window the calendar had moved. It degraded the whole weekly run.
    """

    @patch("health_checker.boto3")
    def test_a_three_day_old_partition_is_found_not_reported_missing(self, mock_boto3):
        """The exact shape that failed: Friday's features read on a Monday."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        s3.head_object.side_effect = Exception("NoSuchKey")

        three_days_ago = (date.today() - timedelta(days=3)).isoformat()
        paginator = MagicMock()

        def paginate(Bucket, Prefix, MaxKeys=100):
            if Prefix == "features/":
                yield {"Contents": [
                    {"Key": f"features/{three_days_ago}/technical.parquet"}
                ]}
            else:
                yield {"Contents": []}

        paginator.paginate.side_effect = paginate
        s3.get_paginator.return_value = paginator

        features = next(r for r in check_all() if r["check"] == "features")
        assert features["status"] != "missing", (
            "a partition three days old was reported as never having existed — "
            "the probe cannot see what its own threshold accepts"
        )
        assert features["age_days"] == 3
        assert features["last_updated"] == three_days_ago

    def test_the_threshold_covers_a_weekday_producer_across_a_weekend(self):
        """features runs Mon-Fri, so Friday's partition is 3 days old on Monday."""
        from health_checker import THRESHOLDS

        assert THRESHOLDS["features"] >= 3, (
            "a Mon-Fri producer's newest partition is up to three calendar "
            "days old on a Monday morning; a threshold below 3 fails every "
            "weekend on data that is exactly as fresh as the producer can "
            "make it (config-I7434)"
        )

    @patch("health_checker.boto3")
    def test_genuinely_absent_features_still_report_missing(self, mock_boto3):
        """The fix must not turn a real producer outage into a pass."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        s3.head_object.side_effect = Exception("NoSuchKey")
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        s3.get_paginator.return_value = paginator

        features = next(r for r in check_all() if r["check"] == "features")
        assert features["status"] == "missing"

    @patch("health_checker.boto3")
    def test_a_partition_beyond_the_threshold_reports_stale_not_missing(self, mock_boto3):
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        s3.head_object.side_effect = Exception("NoSuchKey")

        long_ago = (date.today() - timedelta(days=30)).isoformat()
        paginator = MagicMock()

        def paginate(Bucket, Prefix, MaxKeys=100):
            if Prefix == "features/":
                yield {"Contents": [{"Key": f"features/{long_ago}/technical.parquet"}]}
            else:
                yield {"Contents": []}

        paginator.paginate.side_effect = paginate
        s3.get_paginator.return_value = paginator

        features = next(r for r in check_all() if r["check"] == "features")
        assert features["status"] == "stale", (
            "'stale' and 'missing' are different findings — one says the "
            "producer is behind, the other says it never ran"
        )
