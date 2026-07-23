"""Tests for loaders/watch_efficacy.py (config#2389).

Mirrors tests/test_watch_status_page.py: streamlit is mocked (cache_data ->
passthrough) before import, and S3 reads are patched at the loaders.s3_loader
functions watch_efficacy imports (list_saturday_sf_watch_dates /
load_saturday_sf_watch / list_ci_watch_dates / load_ci_watch /
load_latest_sf_watch_canary / load_latest_ci_watch_canary) — same
patch-the-source-module pattern as the existing S3 loader tests, since
watch_efficacy imports those names directly (patching the re-exported name
in loaders.watch_efficacy, not the original in loaders.s3_loader).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import watch_efficacy as we  # noqa: E402


def _patch_all(
    *,
    sf_dates=(),
    sf_docs=None,
    ci_dates=(),
    ci_docs=None,
    sf_canary=None,
    ci_canary=None,
):
    """Return a contextlib.ExitStack-friendly list of patch objects covering
    every S3-touching name watch_efficacy calls, so a test only needs to
    override the pieces it cares about."""
    sf_docs = sf_docs or {}
    ci_docs = ci_docs or {}

    def _sf_loader(date):
        return sf_docs.get(date)

    def _ci_loader(date):
        return ci_docs.get(date)

    return [
        patch.object(we, "list_saturday_sf_watch_dates", return_value=list(sf_dates)),
        patch.object(we, "load_saturday_sf_watch", side_effect=_sf_loader),
        patch.object(we, "list_ci_watch_dates", return_value=list(ci_dates)),
        patch.object(we, "load_ci_watch", side_effect=_ci_loader),
        patch.object(we, "load_latest_sf_watch_canary", return_value=sf_canary),
        patch.object(we, "load_latest_ci_watch_canary", return_value=ci_canary),
    ]


class _MultiPatch:
    """Small ExitStack-lite helper so tests can list patches declaratively."""

    def __init__(self, patchers):
        self._patchers = patchers

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()
        return False


class TestZeroDates:
    def test_zero_dates_returns_zero_valued_metrics_no_crash(self):
        # canary.total_expected_drills is intentionally derived from
        # wall-clock "now" vs. CANARY_EXPECTED_FROM (config#2223 weekly
        # synthetic-drill cadence), not from the sf_watch/ci_watch `dates`
        # input this test is otherwise exercising — so asserting an
        # all-zero snapshot must isolate that wall-clock dependency the
        # same way TestCanaryEfficacy.test_before_expected_date_shows_zero_expected
        # does, rather than relying on the suite happening to run before
        # CANARY_EXPECTED_FROM. Without this, the test silently broke the
        # day CANARY_EXPECTED_FROM's date (2026-07-23) arrived.
        with patch.object(we, "CANARY_EXPECTED_FROM", "2099-01-01"):
            with _MultiPatch(_patch_all()):
                snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_dates == 0
        assert snap.sf_watch.total_events == 0
        assert snap.sf_watch.fix_rate == 0.0
        assert snap.sf_watch.escalation_rate == 0.0
        assert snap.sf_watch.observe_rate == 0.0
        assert snap.sf_watch.post_autonomy_fix_rate is None
        assert snap.sf_watch.mttr_hours is None
        assert snap.sf_watch.top_failure_modes == []
        assert snap.sf_watch.events_per_date == []

        assert snap.ci_watch.total_dates == 0
        assert snap.ci_watch.total_events == 0
        assert snap.ci_watch.fix_rate == 0.0
        assert snap.ci_watch.rerun_success_rate == 0.0
        assert snap.ci_watch.per_repo == {}

        assert snap.canary.sf_watch_age_days is None
        assert snap.canary.ci_watch_age_days is None
        assert snap.canary.total_expected_drills == 0
        assert snap.canary.reliability == 0.0

        assert snap.computed_at is not None

    def test_dates_present_but_zero_events_still_no_crash(self):
        sf_docs = {"2026-06-20": {"schema_version": 1, "events": []}}
        with _MultiPatch(_patch_all(sf_dates=["2026-06-20"], sf_docs=sf_docs)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_dates == 1
        assert snap.sf_watch.total_events == 0
        assert snap.sf_watch.fix_rate == 0.0
        assert snap.sf_watch.events_per_date == [("2026-06-20", 0, 0)]


class TestMultipleDatesSfWatch:
    def _events(self, *actions_and_states):
        return [
            {"detected_at": f"2026-07-08T{10+i:02d}:00:00Z", "action": a,
             "failed_state": s}
            for i, (a, s) in enumerate(actions_and_states)
        ]

    def test_fix_and_escalation_rates(self):
        sf_docs = {
            "2026-07-08": {
                "events": self._events(
                    ("auto_fixed", "TIMED_OUT"),
                    ("escalated", "FAILED"),
                    ("observe", "ABORTED"),
                    ("merged", "TIMED_OUT"),
                ),
            },
        }
        with _MultiPatch(_patch_all(sf_dates=["2026-07-08"], sf_docs=sf_docs)):
            snap = we.load_watch_efficacy_snapshot()

        sf = snap.sf_watch
        assert sf.total_dates == 1
        assert sf.total_events == 4
        assert sf.fix_rate == 2 / 4
        assert sf.escalation_rate == 1 / 4
        assert sf.observe_rate == 1 / 4
        # top failure modes: TIMED_OUT appears twice, others once each
        assert sf.top_failure_modes[0] == ("TIMED_OUT", 2)

    def test_post_autonomy_fix_rate_filters_by_cutoff_date(self):
        sf_docs = {
            # pre-autonomy date: only observe actions
            "2026-06-13": {"events": self._events(("observe", "FAILED"))},
            # post-autonomy date (>= 2026-07-07): all fixed
            "2026-07-11": {
                "events": self._events(
                    ("auto_fixed", "FAILED"), ("merged", "FAILED"),
                ),
            },
        }
        with _MultiPatch(_patch_all(
            sf_dates=["2026-07-11", "2026-06-13"], sf_docs=sf_docs,
        )):
            snap = we.load_watch_efficacy_snapshot()

        # overall fix_rate is diluted by the pre-autonomy observe-only date
        assert snap.sf_watch.fix_rate == 2 / 3
        # post_autonomy_fix_rate only counts the >= 2026-07-07 date
        assert snap.sf_watch.post_autonomy_fix_rate == 1.0

    def test_mttr_hours_within_date_first_event_to_first_fix(self):
        sf_docs = {
            "2026-07-08": {
                "events": [
                    {"detected_at": "2026-07-08T10:00:00Z", "action": "proposed",
                     "failed_state": "FAILED"},
                    {"detected_at": "2026-07-08T13:30:00Z", "action": "auto_fixed",
                     "failed_state": "FAILED"},
                ],
            },
        }
        with _MultiPatch(_patch_all(sf_dates=["2026-07-08"], sf_docs=sf_docs)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.mttr_hours == 3.5

    def test_mttr_none_when_no_date_has_both_first_event_and_fix(self):
        sf_docs = {
            "2026-07-08": {
                "events": [
                    {"detected_at": "2026-07-08T10:00:00Z", "action": "escalated",
                     "failed_state": "FAILED"},
                ],
            },
        }
        with _MultiPatch(_patch_all(sf_dates=["2026-07-08"], sf_docs=sf_docs)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.mttr_hours is None

    def test_events_per_date_ordering_matches_input_dates(self):
        sf_docs = {
            "2026-07-08": {"events": self._events(("auto_fixed", "FAILED"))},
            "2026-07-01": {"events": self._events(
                ("auto_fixed", "FAILED"), ("proposed", "FAILED"),
            )},
        }
        with _MultiPatch(_patch_all(
            sf_dates=["2026-07-08", "2026-07-01"], sf_docs=sf_docs,
        )):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.events_per_date == [
            ("2026-07-08", 1, 1),
            ("2026-07-01", 2, 1),
        ]


class TestMultipleDatesCiWatch:
    def test_per_repo_and_fix_rate(self):
        ci_docs = {
            "2026-07-02": {
                "events": [
                    {"repo": "crucible-dashboard", "action": "auto_fixed"},
                    {"repo": "crucible-dashboard", "action": "escalated"},
                    {"repo": "alpha-engine-config", "action": "merged"},
                ],
            },
        }
        with _MultiPatch(_patch_all(ci_dates=["2026-07-02"], ci_docs=ci_docs)):
            snap = we.load_watch_efficacy_snapshot()

        ci = snap.ci_watch
        assert ci.total_events == 3
        assert ci.fix_rate == 2 / 3
        assert ci.per_repo == {"crucible-dashboard": 2, "alpha-engine-config": 1}

    def test_rerun_success_rate(self):
        ci_docs = {
            "2026-07-02": {
                "events": [
                    {"repo": "r1", "action": "rerun", "rerun_conclusion": "success"},
                    {"repo": "r1", "action": "rerun", "rerun_conclusion": "failure"},
                    {"repo": "r1", "action": "proposed"},  # no rerun_conclusion
                ],
            },
        }
        with _MultiPatch(_patch_all(ci_dates=["2026-07-02"], ci_docs=ci_docs)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.ci_watch.rerun_success_rate == 0.5


class TestCanaryEfficacy:
    def test_before_expected_date_shows_zero_expected(self):
        # CANARY_EXPECTED_FROM is 2026-07-23; as long as "now" is before
        # that (true for any date this suite runs before that date),
        # total_expected_drills must be 0 regardless of heartbeat presence.
        with patch.object(we, "CANARY_EXPECTED_FROM", "2099-01-01"):
            with _MultiPatch(_patch_all(
                sf_canary={"date": "2026-06-01", "drill_at": "2026-06-01T00:00:00Z"},
            )):
                snap = we.load_watch_efficacy_snapshot()

        assert snap.canary.total_expected_drills == 0
        assert snap.canary.reliability == 0.0

    def test_after_expected_date_counts_present_heartbeats(self):
        with patch.object(we, "CANARY_EXPECTED_FROM", "2020-01-01"):
            with _MultiPatch(_patch_all(
                sf_canary={"date": "2026-06-01", "drill_at": "2026-06-01T00:00:00Z"},
                ci_canary=None,
            )):
                snap = we.load_watch_efficacy_snapshot()

        assert snap.canary.total_expected_drills == 2
        assert snap.canary.successful_drills == 1
        assert snap.canary.reliability == 0.5

    def test_age_days_computed_from_drill_at(self):
        sf_canary = {"date": "2026-07-06", "drill_at": "2026-07-06T00:00:00Z"}
        with _MultiPatch(_patch_all(sf_canary=sf_canary)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.canary.sf_watch_last_heartbeat == "2026-07-06"
        assert snap.canary.sf_watch_age_days is not None
        assert snap.canary.sf_watch_age_days > 0

    def test_no_heartbeat_is_none_not_crash(self):
        with _MultiPatch(_patch_all(sf_canary=None, ci_canary=None)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.canary.sf_watch_age_days is None
        assert snap.canary.ci_watch_age_days is None


class TestPartialReadFailure:
    def test_one_date_returns_none_others_still_aggregate(self):
        # load_saturday_sf_watch returning None for a date mirrors
        # s3_loader's own missing-key / JSON-parse-error contract (both
        # collapse to None) — the aggregator must skip it, not crash.
        sf_docs = {
            "2026-07-08": {
                "events": [
                    {"detected_at": "2026-07-08T10:00:00Z", "action": "auto_fixed",
                     "failed_state": "FAILED"},
                ],
            },
            "2026-06-13": None,  # unreadable / parse-error date
        }
        with _MultiPatch(_patch_all(
            sf_dates=["2026-07-08", "2026-06-13"], sf_docs=sf_docs,
        )):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_dates == 1
        assert snap.sf_watch.total_events == 1
        assert snap.sf_watch.fix_rate == 1.0
        assert snap.sf_watch.events_per_date == [("2026-07-08", 1, 1)]

    def test_malformed_doc_missing_events_key_is_skipped(self):
        sf_docs = {
            "2026-07-08": {"schema_version": 1},  # no "events" key at all
        }
        with _MultiPatch(_patch_all(sf_dates=["2026-07-08"], sf_docs=sf_docs)):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_dates == 0
        assert snap.sf_watch.total_events == 0

    def test_loader_raising_is_caught_and_skipped(self):
        def _raising_loader(date):
            raise RuntimeError("boom")

        patchers = _patch_all(sf_dates=["2026-07-08", "2026-06-13"])
        # override the load_saturday_sf_watch patch with one that raises
        patchers[1] = patch.object(
            we, "load_saturday_sf_watch", side_effect=_raising_loader
        )
        with _MultiPatch(patchers):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_dates == 0
        assert snap.sf_watch.total_events == 0

    def test_listing_failure_treated_as_zero_dates(self):
        patchers = _patch_all()
        patchers[0] = patch.object(
            we, "list_saturday_sf_watch_dates",
            side_effect=RuntimeError("boom"),
        )
        with _MultiPatch(patchers):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_dates == 0
        assert snap.sf_watch.total_events == 0

    def test_ci_watch_partial_failure_independent_of_sf_watch(self):
        # A CI Watch read failure must not affect the SF Watch aggregate,
        # and vice versa — the two are aggregated independently.
        sf_docs = {
            "2026-07-08": {
                "events": [
                    {"detected_at": "2026-07-08T10:00:00Z", "action": "auto_fixed",
                     "failed_state": "FAILED"},
                ],
            },
        }
        ci_docs = {"2026-07-02": None}
        with _MultiPatch(_patch_all(
            sf_dates=["2026-07-08"], sf_docs=sf_docs,
            ci_dates=["2026-07-02"], ci_docs=ci_docs,
        )):
            snap = we.load_watch_efficacy_snapshot()

        assert snap.sf_watch.total_events == 1
        assert snap.ci_watch.total_dates == 0
        assert snap.ci_watch.total_events == 0


class TestSnapshotStructure:
    def test_dataclass_shapes_exposed(self):
        # Structural check that the dataclasses import + instantiate with
        # their documented defaults (config#2389 acceptance: the module
        # exposes WatchEfficacySnapshot / SfWatchEfficacy / CiWatchEfficacy /
        # CanaryEfficacy).
        assert we.WatchEfficacySnapshot().sf_watch == we.SfWatchEfficacy()
        assert we.WatchEfficacySnapshot().ci_watch == we.CiWatchEfficacy()
        assert we.WatchEfficacySnapshot().canary == we.CanaryEfficacy()

    def test_module_exposes_expected_public_loader(self):
        assert callable(we.load_watch_efficacy_snapshot)
