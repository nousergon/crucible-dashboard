"""Tests for loaders/groom_trends.py — the pure transforms behind the
Backlog Groom page's slot-decision table, trend frames, trailing-window
KPIs, and disposition-audit summary (2026-07-14 readability redesign)."""

from datetime import datetime, timezone

from loaders.groom_trends import (
    TIER_COLOR,
    TIER_ORDER,
    audit_summary,
    decision_table_rows,
    demand_trend_rows,
    is_scheduled_slot,
    runs_trend_rows,
    slot_utc_time,
    window_kpis,
)

KNOWN = ("trigger-0100", "trigger-0700", "trigger-1900")


def _decision_raw(*, decided_at, counts=None, decisions=None, schedule="0 19 * * *"):
    return {
        "schema_version": 2,
        "trigger": "demand-all",
        "schedule": schedule,
        "counts": counts or {"low": 3, "mid": 19, "high": 5},
        "decisions": decisions if decisions is not None else [],
        "decided_at": decided_at,
    }


LAUNCH_BOX = {"launch": True, "tiers": ["low", "mid"], "issue_filter": "mid+low",
              "model": "claude-sonnet-5",
              "reason": "22 actionable across low+mid (anchor mid >= floor 8)"}
DEFER_BOX = {"launch": False, "tiers": ["high"], "issue_filter": "", "model": "",
             "reason": "thin pool high (5) < floor 8, no P0, none waited 72h"}


class TestSlotHelpers:
    def test_slot_utc_time_parses_trigger_names(self):
        assert slot_utc_time("trigger-1900") == (19, 0)
        assert slot_utc_time("trigger-0100") == (1, 0)

    def test_slot_utc_time_rejects_ad_hoc_names(self):
        assert slot_utc_time("sweep-175017") is None
        assert slot_utc_time("trigger-9999") is None

    def test_is_scheduled_slot_excludes_manual_triggers(self):
        # A hand-run trigger (e.g. trigger-1404 pace-gate test) must never
        # join the scheduled set — before this split it spawned phantom
        # "missing record" cells for every other day in the window.
        assert is_scheduled_slot("trigger-1900", KNOWN)
        assert not is_scheduled_slot("trigger-1404", KNOWN)


class TestDecisionTableRows:
    NOW = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    def test_renders_counts_launched_and_deferred(self):
        raw = _decision_raw(decided_at="2026-07-14T19:00:35+00:00",
                            decisions=[LAUNCH_BOX, DEFER_BOX])
        rows = decision_table_rows(
            [("groom/decisions/2026-07-14/trigger-1900.json", raw,
              raw["decisions"])],
            known_slots=KNOWN, now=self.NOW, days=1,
        )
        launched_row = next(r for r in rows if r["Slot"] == "trigger-1900")
        assert launched_row["low"] == 3 and launched_row["mid"] == 19 and launched_row["high"] == 5
        assert "mid+low → sonnet-5" in launched_row["Launched"]
        assert "thin pool high" in launched_row["Deferred"]
        assert launched_row["Status"] == "🟢 launched 1"
        assert launched_row["Type"] == "scheduled"

    def test_manual_trigger_labeled_and_never_flagged_missing_elsewhere(self):
        raw = _decision_raw(decided_at="2026-07-14T14:04:13+00:00",
                            schedule="manual-test", decisions=[LAUNCH_BOX])
        rows = decision_table_rows(
            [("groom/decisions/2026-07-14/trigger-1404.json", raw,
              raw["decisions"])],
            known_slots=KNOWN, now=self.NOW, days=3,
        )
        manual = [r for r in rows if r["Slot"] == "trigger-1404"]
        assert len(manual) == 1  # exactly the real record — no phantom rows
        assert manual[0]["Type"] == "🔧 manual"

    def test_due_scheduled_slot_without_record_is_flagged(self):
        rows = decision_table_rows(
            [], known_slots=KNOWN, now=self.NOW, days=1,
        )
        # At 20:00 UTC all three of today's slots are due and missing.
        assert sum(1 for r in rows if r["Status"] == "⚠️ NO RECORD") == 3

    def test_slot_later_today_is_not_flagged(self):
        early = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)
        rows = decision_table_rows(
            [], known_slots=KNOWN, now=early, days=1,
        )
        flagged = {r["Slot"] for r in rows if r["Status"] == "⚠️ NO RECORD"}
        # 01:00 is due (+grace); 07:00 and 19:00 have not arrived yet.
        assert flagged == {"trigger-0100"}

    def test_full_skip_record_is_distinct_from_missing(self):
        raw = _decision_raw(decided_at="2026-07-14T19:00:35+00:00", decisions=[])
        rows = decision_table_rows(
            [("groom/decisions/2026-07-14/trigger-1900.json", raw, [])],
            known_slots=KNOWN, now=self.NOW, days=1,
        )
        skip_row = next(r for r in rows if r["Slot"] == "trigger-1900")
        assert skip_row["Status"] == "⚪ full skip"

    def test_rows_sorted_newest_first(self):
        raw_old = _decision_raw(decided_at="2026-07-13T19:00:00+00:00",
                                decisions=[LAUNCH_BOX])
        raw_new = _decision_raw(decided_at="2026-07-14T19:00:00+00:00",
                                decisions=[LAUNCH_BOX])
        rows = decision_table_rows(
            [("groom/decisions/2026-07-13/trigger-1900.json", raw_old, [LAUNCH_BOX]),
             ("groom/decisions/2026-07-14/trigger-1900.json", raw_new, [LAUNCH_BOX])],
            known_slots=("trigger-1900",), now=self.NOW, days=2,
        )
        real = [r for r in rows if r["Status"].startswith("🟢")]
        assert real[0]["When (UTC)"].startswith("2026-07-14")


class TestDemandTrendRows:
    def test_oldest_first_with_tier_counts(self):
        recs = [
            ("k2", _decision_raw(decided_at="2026-07-14T19:00:00+00:00",
                                 counts={"low": 3, "mid": 19, "high": 5})),
            ("k1", _decision_raw(decided_at="2026-07-14T07:00:00+00:00",
                                 counts={"low": 19, "mid": 42, "high": 17})),
        ]
        rows = demand_trend_rows(recs)
        assert [r["mid"] for r in rows] == [42, 19]

    def test_skips_records_without_counts_or_timestamp(self):
        assert demand_trend_rows([("k", {"decided_at": "2026-07-14T07:00:00+00:00"})]) == []
        assert demand_trend_rows([("k", {"counts": {"low": 1}})]) == []


def _run_doc(**over):
    doc = {
        "run_kind": "coverage",
        "run_start": "2026-07-14T14:06:59Z",
        "issue_filter": "mid-only",
        "total_issues": 47,
        "engaged": 39,
        "undispositioned": 8,
        "dropped_at_cap": 0,
        "gated_excluded": 165,
        "max_turns_chunks": 6,
        "floor_fail": False,
        "issues": [{"disposition": "closed"}, {"disposition": "pr_opened"},
                   {"disposition": "commented"}],
    }
    doc.update(over)
    return doc


class TestRunsTrendRows:
    def test_excludes_sweep_runs(self):
        eff = {"engaged": 39, "wet_per_engaged": 964145.3}
        rows = runs_trend_rows([
            ("groom/2026-07-14/a.json", _run_doc(), eff),
            ("groom/2026-07-14/s.json", _run_doc(run_kind="sweep"), eff),
        ])
        assert len(rows) == 1

    def test_carries_v9_queue_shape_fields(self):
        eff = {"engaged": 39, "wet_per_engaged": 964145.3}
        (row,) = runs_trend_rows([("groom/2026-07-14/a.json", _run_doc(), eff)])
        assert row["undispositioned"] == 8
        assert row["max_turns_chunks"] == 6
        assert row["tier"] == "mid"
        assert round(row["wet_per_engaged_k"]) == 964
        assert round(row["coverage_pct"]) == 83

    def test_mixed_filter_anchors_on_dominant_tier(self):
        eff = {"engaged": 10, "wet_per_engaged": None}
        (row,) = runs_trend_rows(
            [("k", _run_doc(issue_filter="mid+low"), eff)])
        assert row["tier"] == "mid"


class TestWindowKpis:
    NOW = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    def test_sums_window_and_excludes_older_runs(self):
        eff = {"engaged": 39, "wet_per_engaged": 900_000.0}
        rows = runs_trend_rows([
            ("in", _run_doc(), eff),
            ("out", _run_doc(run_start="2026-06-01T00:00:00Z"), eff),
        ])
        kpis = window_kpis(rows, now=self.NOW, days=7)
        assert kpis["runs"] == 1
        assert kpis["engaged"] == 39
        assert kpis["undispositioned"] == 8
        assert kpis["max_turns_chunks"] == 6
        assert kpis["median_wet_per_engaged_k"] == 900.0

    def test_empty_window_is_all_zeroes_not_error(self):
        kpis = window_kpis([], now=self.NOW, days=7)
        assert kpis["runs"] == 0
        assert kpis["median_wet_per_engaged_k"] is None


class TestAuditSummary:
    NOW = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    def _doc(self, **over):
        doc = {"schema_version": 1, "date": "2026-07-10", "window_days": 7,
               "samples": [{}] * 10, "pass_count": 9, "fail_count": 1,
               "error_count": 0}
        doc.update(over)
        return doc

    def test_missing_artifact(self):
        assert audit_summary(None, None, now=self.NOW)["status"] == "missing"

    def test_single_fail_is_warn(self):
        s = audit_summary("groom/audit/2026-07-10.json", self._doc(), now=self.NOW)
        assert s["status"] == "warn"
        assert s["sampled"] == 10

    def test_two_fails_is_fail(self):
        s = audit_summary("groom/audit/2026-07-10.json",
                          self._doc(fail_count=2, pass_count=8), now=self.NOW)
        assert s["status"] == "fail"

    def test_clean_is_ok(self):
        s = audit_summary("groom/audit/2026-07-10.json",
                          self._doc(fail_count=0, pass_count=10), now=self.NOW)
        assert s["status"] == "ok"

    def test_stale_beats_verdict(self):
        s = audit_summary("groom/audit/2026-07-01.json",
                          self._doc(date="2026-07-01", fail_count=0,
                                    pass_count=10), now=self.NOW)
        assert s["status"] == "stale"


class TestTierPalette:
    def test_fixed_assignment_covers_all_tiers(self):
        # Categorical color follows the entity (tier), never its rank —
        # every tier has a fixed hex that filtering must not reassign.
        assert set(TIER_ORDER) == set(TIER_COLOR)
        assert len(set(TIER_COLOR.values())) == len(TIER_COLOR)


class TestSkipReasonRecords:
    NOW = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    def test_skip_reason_record_renders_distinct_status(self):
        # config-I2540: dispatcher ran but enumeration failed — must render
        # distinctly from both "⚪ full skip" (demand-based) and
        # "⚠️ NO RECORD" (scheduler outage).
        raw = {
            "schema_version": 2, "trigger": "demand-all",
            "schedule": "0 19 * * *", "skip_reason": "demand_all_failed",
            "decisions": [], "error": "github down",
            "decided_at": "2026-07-14T19:00:35+00:00",
        }
        rows = decision_table_rows(
            [("groom/decisions/2026-07-14/trigger-1900.json", raw, [])],
            known_slots=KNOWN, now=self.NOW, days=1,
        )
        row = next(r for r in rows if r["Slot"] == "trigger-1900")
        assert row["Status"] == "🔴 demand_all_failed"
        assert "github down" in row["Deferred"]

    def test_empty_decisions_without_skip_reason_stays_full_skip(self):
        raw = {"schema_version": 2, "trigger": "demand-all",
               "schedule": "0 19 * * *", "decisions": [],
               "counts": {"low": 1, "mid": 2, "high": 3},
               "decided_at": "2026-07-14T19:00:35+00:00"}
        rows = decision_table_rows(
            [("groom/decisions/2026-07-14/trigger-1900.json", raw, [])],
            known_slots=KNOWN, now=self.NOW, days=1,
        )
        row = next(r for r in rows if r["Slot"] == "trigger-1900")
        assert row["Status"] == "⚪ full skip"
