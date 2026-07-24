"""Frozen-clock tests for the Fleet SLA resolver (sla_status.py).

The resolver is pure — every MET/BREACHED/PENDING/NOT_EXPECTED verdict is
a deterministic function of an SlaInputs snapshot — so the full verdict
matrix is exercised here without AWS, S3, or a live clock. Mirrors
``tests/test_fleet_status.py``'s frozen-clock pattern.

Reference clocks (2026, EDT — market 13:30–20:00 UTC):
  TRADING_MID  Tue 2026-07-07 15:00 UTC — mid-session on a trading day
  SATURDAY     Sat 2026-07-11 10:00 UTC — after the weekly 09:00 cron (+grace)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sla_status import (  # noqa: E402
    BREACHED,
    MET,
    NOT_EXPECTED,
    PENDING,
    SlaInputs,
    SlaRegistryRow,
    most_recent_cron_firing,
    resolve_process,
    resolve_sla_table,
    worst_verdict,
)

TRADING_MID = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)  # Tuesday
SATURDAY = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)


def _reg(**kw) -> SlaRegistryRow:
    defaults = dict(
        artifact_id="a", cadence="weekday_sf", sla_minutes_after_cron=60,
        owner_repo="r", severity="warning",
    )
    defaults.update(kw)
    return SlaRegistryRow(**defaults)


# ── Cadence window math ──────────────────────────────────────────────────────


class TestMostRecentCronFiring:
    def test_weekday_sf_same_day_after_cron(self):
        firing = most_recent_cron_firing("weekday_sf", TRADING_MID)
        assert firing == datetime(2026, 7, 7, 12, 45, tzinfo=timezone.utc)

    def test_weekday_sf_before_cron_falls_back_to_prior_day(self):
        early = datetime(2026, 7, 7, 8, 0, tzinfo=timezone.utc)
        firing = most_recent_cron_firing("weekday_sf", early)
        assert firing == datetime(2026, 7, 6, 12, 45, tzinfo=timezone.utc)  # Monday

    def test_weekday_sf_monday_skips_weekend_and_holiday(self):
        # Mon 2026-07-06 pre-cron: back over the weekend AND the Fri 07-03
        # Independence-Day-observed holiday lands on Thu 2026-07-02.
        monday_early = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
        firing = most_recent_cron_firing("weekday_sf", monday_early)
        assert firing == datetime(2026, 7, 2, 12, 45, tzinfo=timezone.utc)  # Thursday

    def test_saturday_sf_after_cron(self):
        firing = most_recent_cron_firing("saturday_sf", SATURDAY)
        assert firing == datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)

    def test_saturday_sf_midweek_finds_prior_saturday(self):
        firing = most_recent_cron_firing("saturday_sf", TRADING_MID)
        assert firing == datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)

    def test_eod_sf_after_close(self):
        after_close = datetime(2026, 7, 7, 21, 0, tzinfo=timezone.utc)
        firing = most_recent_cron_firing("eod_sf", after_close)
        assert firing == datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)

    def test_continuous_has_no_firing(self):
        assert most_recent_cron_firing("continuous", TRADING_MID) is None

    def test_unknown_cadence_has_no_firing(self):
        assert most_recent_cron_firing("bogus", TRADING_MID) is None


# ── Verdict matrix ───────────────────────────────────────────────────────────


class TestResolveProcess:
    def test_fresh_state_is_met(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "fresh", "last_modified": None,
                     "reason": ""},
            None, TRADING_MID,
        )
        assert row.verdict == MET

    def test_stale_state_is_breached(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "stale", "reason": "past SLA"},
            None, TRADING_MID,
        )
        assert row.verdict == BREACHED

    def test_missing_state_is_breached(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "missing", "reason": "absent"},
            None, TRADING_MID,
        )
        assert row.verdict == BREACHED

    def test_probe_failed_is_breached(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "probe_failed", "reason": "boom"},
            None, TRADING_MID,
        )
        assert row.verdict == BREACHED

    def test_grace_period_is_pending(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "grace_period", "reason": "cold start"},
            None, TRADING_MID,
        )
        assert row.verdict == PENDING

    def test_no_check_row_before_due_is_pending(self):
        early = datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc)  # cron 12:45 + SLA 60m
        row = resolve_process(_reg(sla_minutes_after_cron=60), None, None, early)
        assert row.verdict == PENDING

    def test_no_check_row_past_due_is_breached(self):
        late = datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc)  # past 13:45 due
        row = resolve_process(_reg(sla_minutes_after_cron=60), None, None, late)
        assert row.verdict == BREACHED

    def test_unknown_cadence_is_not_expected(self):
        row = resolve_process(_reg(cadence="bogus"), None, None, TRADING_MID)
        assert row.verdict == NOT_EXPECTED

    def test_continuous_with_no_check_row_is_not_expected(self):
        row = resolve_process(_reg(cadence="continuous"), None, None, TRADING_MID)
        assert row.verdict == NOT_EXPECTED
        assert row.last_expected_utc is None

    def test_continuous_fresh_is_met(self):
        row = resolve_process(
            _reg(cadence="continuous"),
            {"artifact_id": "a", "state": "fresh", "last_modified": "2026-07-07T14:00:00Z"},
            None, TRADING_MID,
        )
        assert row.verdict == MET
        assert row.last_completed_utc == datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc)

    def test_last_expected_utc_is_firing_plus_sla(self):
        row = resolve_process(_reg(sla_minutes_after_cron=60), None, None, TRADING_MID)
        assert row.last_expected_utc == datetime(2026, 7, 7, 13, 45, tzinfo=timezone.utc)

    def test_trigger_and_pipeline_labels(self):
        row = resolve_process(_reg(cadence="saturday_sf"), None, None, SATURDAY)
        assert "Sat" in row.trigger
        assert "Weekly" in row.pipeline


# ── Hit-rate ─────────────────────────────────────────────────────────────────


class TestHitRate:
    def test_continuous_gaps_lower_hit_rate(self):
        row = resolve_process(
            _reg(),
            {"artifact_id": "a", "state": "fresh"},
            {"gap_count": 3, "lookback_cycles": 30, "is_latest_pointer": False},
            TRADING_MID,
        )
        assert row.hit_rate_30d == round(27 / 30, 4)
        assert row.lookback_cycles == 30

    def test_no_gaps_is_perfect_hit_rate(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "fresh"},
            {"gap_count": 0, "lookback_cycles": 12, "is_latest_pointer": False},
            SATURDAY,
        )
        assert row.hit_rate_30d == 1.0

    def test_latest_pointer_has_no_hit_rate(self):
        row = resolve_process(
            _reg(), {"artifact_id": "a", "state": "fresh"},
            {"is_latest_pointer": True, "history": [{"present": True}]},
            TRADING_MID,
        )
        assert row.hit_rate_30d is None

    def test_absent_history_entry_has_no_hit_rate(self):
        row = resolve_process(_reg(), {"artifact_id": "a", "state": "fresh"}, None, TRADING_MID)
        assert row.hit_rate_30d is None


# ── Table assembly + rollup ──────────────────────────────────────────────────


class TestResolveSlaTable:
    def test_joins_by_artifact_id(self):
        inp = SlaInputs(
            now=TRADING_MID,
            registry=(_reg(artifact_id="a"), _reg(artifact_id="b", cadence="continuous")),
            check_results={"results": [
                {"artifact_id": "a", "state": "fresh"},
                {"artifact_id": "b", "state": "missing"},
            ]},
            history={"artifacts": {"a": {"gap_count": 1, "lookback_cycles": 30}}},
        )
        rows = resolve_sla_table(inp)
        by_id = {r.process_id: r for r in rows}
        assert by_id["a"].verdict == MET
        assert by_id["a"].hit_rate_30d == round(29 / 30, 4)
        assert by_id["b"].verdict == BREACHED

    def test_missing_check_results_degrades_gracefully(self):
        inp = SlaInputs(now=TRADING_MID, registry=(_reg(artifact_id="a"),))
        rows = resolve_sla_table(inp)
        assert len(rows) == 1
        assert rows[0].verdict in (PENDING, BREACHED)  # time-only fallback, never MET


class TestWorstVerdict:
    def test_empty_table_is_none(self):
        assert worst_verdict([]) is None

    def test_breached_outranks_pending(self):
        met = resolve_process(_reg(), {"artifact_id": "a", "state": "fresh"}, None, TRADING_MID)
        breached = resolve_process(
            _reg(artifact_id="b"), {"artifact_id": "b", "state": "missing"}, None, TRADING_MID,
        )
        assert worst_verdict([met, breached]) == BREACHED
