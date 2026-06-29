"""Unit tests for the morning-brief cadence gates (config#664 / L4574).

These exercise the PURE decision core in ``live/morning_brief_cadence.py`` with
a synthetic clock and synthetic snapshots — NO live market data, NO Anthropic
key, NO S3, NO Streamlit. Each of the four gates is covered, plus the
first-view-of-day backstop, the daily cap, the closed-window path, and the
holiday / pre-open window edges.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

# Pure module lives under live/; add it (and repo root) to the path. No
# streamlit/anthropic/boto3 import occurs from morning_brief_cadence.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE = os.path.join(_ROOT, "live")
for _p in (_ROOT, _LIVE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from morning_brief_cadence import (  # noqa: E402
    BriefState,
    CadenceConfig,
    Decision,
    MarketSnapshot,
    decide,
    is_callable_window,
    is_material_move,
    throttle_elapsed,
)

ET = ZoneInfo("America/New_York")

# A synthetic NYSE-calendar predicate: weekdays are trading days EXCEPT a
# fixed holiday. Keeps the tests independent of trading_calendar's real table.
_HOLIDAY = date(2026, 6, 19)  # Juneteenth 2026 (a Friday)


def fake_is_trading_day(d: date) -> bool:
    if d.weekday() > 4:
        return False
    if d == _HOLIDAY:
        return False
    return True


def et(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def snap(ts, spy=None, qqq=None, vix=None) -> MarketSnapshot:
    return MarketSnapshot(ts=ts, spy_day_return_pp=spy, qqq_day_return_pp=qqq, vix=vix)


CFG = CadenceConfig()  # defaults


# ── Gate 1: callable window (incl. pre-open + holiday) ─────────────────────


class TestCallableWindow:
    def test_during_market_hours_is_open(self):
        assert is_callable_window(
            et(2026, 6, 18, 10, 30), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_pre_open_lead_is_open(self):
        # 08:35 ET, default 30-min lead → window opens at 08:30, so 08:35 is in.
        assert is_callable_window(
            et(2026, 6, 18, 8, 35), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_before_pre_open_lead_is_closed(self):
        # 08:25 ET is before the 08:30 effective open → closed.
        assert not is_callable_window(
            et(2026, 6, 18, 8, 25), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_after_close_is_closed(self):
        assert not is_callable_window(
            et(2026, 6, 18, 16, 1), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_at_close_boundary_is_open(self):
        assert is_callable_window(
            et(2026, 6, 18, 16, 0), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_weekend_is_closed(self):
        # 2026-06-20 is a Saturday.
        assert not is_callable_window(
            et(2026, 6, 20, 10, 0), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_holiday_is_closed_even_during_hours(self):
        assert not is_callable_window(
            et(2026, 6, 19, 10, 0), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_pre_open_lead_is_tunable(self):
        cfg = CadenceConfig(pre_open_lead_min=60)
        # 08:05 ET is inside a 60-min lead (opens 08:00) but outside the 30-min.
        assert is_callable_window(
            et(2026, 6, 18, 8, 5), config=cfg, is_trading_day=fake_is_trading_day
        )
        assert not is_callable_window(
            et(2026, 6, 18, 8, 5), config=CFG, is_trading_day=fake_is_trading_day
        )

    def test_non_et_tz_input_is_converted(self):
        # 14:30 UTC == 10:30 ET (EDT) on a trading day → open.
        utc = ZoneInfo("UTC")
        assert is_callable_window(
            datetime(2026, 6, 18, 14, 30, tzinfo=utc),
            config=CFG,
            is_trading_day=fake_is_trading_day,
        )


# ── Gate 3: hourly throttle ────────────────────────────────────────────────


class TestThrottle:
    def test_within_throttle_not_elapsed(self):
        last = et(2026, 6, 18, 10, 0)
        assert not throttle_elapsed(et(2026, 6, 18, 10, 30), last, config=CFG)

    def test_exactly_at_throttle_boundary_elapsed(self):
        last = et(2026, 6, 18, 10, 0)
        assert throttle_elapsed(et(2026, 6, 18, 11, 0), last, config=CFG)

    def test_past_throttle_elapsed(self):
        last = et(2026, 6, 18, 10, 0)
        assert throttle_elapsed(et(2026, 6, 18, 12, 0), last, config=CFG)


# ── Gate 4: materiality (SPY / QQQ / VIX) ──────────────────────────────────


class TestMateriality:
    def test_immaterial_small_index_move(self):
        base = snap(et(2026, 6, 18, 9, 0), spy=-0.10, qqq=-0.20, vix=15.0)
        cur = snap(et(2026, 6, 18, 11, 0), spy=-0.30, qqq=-0.40, vix=15.5)
        assert not is_material_move(cur, base, config=CFG)

    def test_material_spy_move(self):
        base = snap(et(2026, 6, 18, 9, 0), spy=0.00, qqq=0.00, vix=15.0)
        # SPY moved -0.80 pp since baseline (>= 0.75 default).
        cur = snap(et(2026, 6, 18, 11, 0), spy=-0.80, qqq=-0.10, vix=15.0)
        assert is_material_move(cur, base, config=CFG)

    def test_material_qqq_move_when_spy_quiet(self):
        base = snap(et(2026, 6, 18, 9, 0), spy=0.00, qqq=0.00, vix=15.0)
        # Larger-magnitude leg is QQQ (-1.00 pp).
        cur = snap(et(2026, 6, 18, 11, 0), spy=-0.10, qqq=-1.00, vix=15.0)
        assert is_material_move(cur, base, config=CFG)

    def test_material_vix_jump(self):
        base = snap(et(2026, 6, 18, 9, 0), spy=0.0, qqq=0.0, vix=15.0)
        # VIX jumped +2.5 (>= 2.0 default), index legs flat.
        cur = snap(et(2026, 6, 18, 11, 0), spy=0.0, qqq=0.0, vix=17.5)
        assert is_material_move(cur, base, config=CFG)

    def test_vix_drop_is_not_material(self):
        # The VIX leg is a JUMP (signed), so a fall in VIX must NOT trip it.
        base = snap(et(2026, 6, 18, 9, 0), spy=0.0, qqq=0.0, vix=20.0)
        cur = snap(et(2026, 6, 18, 11, 0), spy=0.0, qqq=0.0, vix=15.0)
        assert not is_material_move(cur, base, config=CFG)

    def test_missing_legs_cannot_trip(self):
        base = snap(et(2026, 6, 18, 9, 0), spy=None, qqq=None, vix=None)
        cur = snap(et(2026, 6, 18, 11, 0), spy=-5.0, qqq=-5.0, vix=40.0)
        assert not is_material_move(cur, base, config=CFG)

    def test_thresholds_are_tunable(self):
        base = snap(et(2026, 6, 18, 9, 0), spy=0.0, qqq=0.0, vix=15.0)
        cur = snap(et(2026, 6, 18, 11, 0), spy=-0.50, qqq=-0.50, vix=15.0)
        # -0.50 is immaterial at default 0.75 but material at a 0.40 threshold.
        assert not is_material_move(cur, base, config=CFG)
        assert is_material_move(cur, base, config=CadenceConfig(material_index_pp=0.40))


# ── decide(): orchestration of all four gates + backstops ──────────────────


class TestDecide:
    def _decide(self, now, current, last):
        return decide(
            now=now,
            current_snapshot=current,
            last_state=last,
            is_trading_day=fake_is_trading_day,
            config=CFG,
        )

    def test_closed_outside_window_never_generates(self):
        now = et(2026, 6, 18, 7, 0)  # pre pre-open
        cur = snap(now, spy=-1.0, qqq=-1.0, vix=25.0)
        res = self._decide(now, cur, None)
        assert res.decision is Decision.CLOSED

    def test_closed_on_holiday(self):
        now = et(2026, 6, 19, 10, 0)
        res = self._decide(now, snap(now, spy=-2.0), None)
        assert res.decision is Decision.CLOSED

    def test_first_view_of_day_generates(self):
        now = et(2026, 6, 18, 9, 5)
        res = self._decide(now, snap(now, spy=-0.1, qqq=-0.1, vix=15.0), None)
        assert res.decision is Decision.GENERATE
        assert res.reason == "first_view_of_trading_day"

    def test_first_view_pre_open_in_lead_generates(self):
        # In the pre-open lead band, first view of the day still generates.
        now = et(2026, 6, 18, 8, 40)
        res = self._decide(now, snap(now, spy=0.0), None)
        assert res.decision is Decision.GENERATE

    def test_first_view_without_snapshot_reuses(self):
        now = et(2026, 6, 18, 9, 5)
        res = self._decide(now, None, None)
        assert res.decision is Decision.REUSE_CACHED
        assert res.reason == "first_view_no_snapshot"

    def test_within_throttle_reuses_even_if_material(self):
        gen_at = et(2026, 6, 18, 9, 5)
        base = snap(gen_at, spy=0.0, qqq=0.0, vix=15.0)
        last = BriefState(date(2026, 6, 18), "brief", base, gen_at, call_count=1)
        now = et(2026, 6, 18, 9, 40)  # 35 min later, < 60-min throttle
        cur = snap(now, spy=-3.0, qqq=-3.0, vix=30.0)  # very material
        res = self._decide(now, cur, last)
        assert res.decision is Decision.REUSE_CACHED
        assert res.reason == "within_throttle_window"

    def test_past_throttle_immaterial_reuses(self):
        gen_at = et(2026, 6, 18, 9, 5)
        base = snap(gen_at, spy=-0.1, qqq=-0.1, vix=15.0)
        last = BriefState(date(2026, 6, 18), "brief", base, gen_at, call_count=1)
        now = et(2026, 6, 18, 10, 30)  # > 60 min
        cur = snap(now, spy=-0.2, qqq=-0.2, vix=15.1)  # immaterial
        res = self._decide(now, cur, last)
        assert res.decision is Decision.REUSE_CACHED
        assert res.reason == "immaterial_move"

    def test_past_throttle_material_generates(self):
        gen_at = et(2026, 6, 18, 9, 5)
        base = snap(gen_at, spy=0.0, qqq=0.0, vix=15.0)
        last = BriefState(date(2026, 6, 18), "brief", base, gen_at, call_count=1)
        now = et(2026, 6, 18, 10, 30)
        cur = snap(now, spy=-1.0, qqq=-0.2, vix=15.0)  # SPY -1.0 pp, material
        res = self._decide(now, cur, last)
        assert res.decision is Decision.GENERATE
        assert res.reason == "material_move"

    def test_daily_cap_blocks_further_generation(self):
        gen_at = et(2026, 6, 18, 9, 5)
        base = snap(gen_at, spy=0.0, qqq=0.0, vix=15.0)
        # Already at the cap (8 calls).
        last = BriefState(date(2026, 6, 18), "brief", base, gen_at, call_count=8)
        now = et(2026, 6, 18, 15, 0)  # well past throttle
        cur = snap(now, spy=-5.0, qqq=-5.0, vix=40.0)  # extremely material
        res = self._decide(now, cur, last)
        assert res.decision is Decision.REUSE_CACHED
        assert res.reason == "daily_cap_reached"

    def test_prior_day_state_is_treated_as_first_view_today(self):
        # A brief from YESTERDAY must not satisfy "first view today" suppression
        # nor count against today's cap → today's first view generates.
        y_gen = et(2026, 6, 17, 15, 0)
        y_state = BriefState(
            date(2026, 6, 17), "yesterday", snap(y_gen, spy=0.0), y_gen, call_count=8
        )
        now = et(2026, 6, 18, 9, 5)
        res = self._decide(now, snap(now, spy=-0.1, qqq=-0.1, vix=15.0), y_state)
        assert res.decision is Decision.GENERATE
        assert res.reason == "first_view_of_trading_day"

    def test_demand_no_background_caller(self):
        # Gate 2 (demand) is structural: decide() only fires from a rerun. There
        # is no cron path; a CLOSED result outside the window proves no call is
        # made without a viewer present inside the window. (Documented invariant.)
        now = et(2026, 6, 21, 12, 0)  # Sunday — no viewer-driven call possible
        res = self._decide(now, snap(now, spy=-9.0), None)
        assert res.decision is Decision.CLOSED


# ── Serialization round-trip (persistence keyed by trading_day) ────────────


class TestSerialization:
    def test_brief_state_round_trip(self):
        gen_at = et(2026, 6, 18, 9, 5)
        st_ = BriefState(
            trading_day=date(2026, 6, 18),
            brief_text="hello",
            snapshot=snap(gen_at, spy=-0.5, qqq=-0.7, vix=16.0),
            generated_at=gen_at,
            call_count=3,
        )
        back = BriefState.from_dict(st_.to_dict())
        assert back.trading_day == st_.trading_day
        assert back.brief_text == "hello"
        assert back.call_count == 3
        assert back.snapshot.spy_day_return_pp == -0.5
        assert back.snapshot.vix == 16.0
        assert back.generated_at == gen_at

    def test_market_snapshot_round_trip_with_none_legs(self):
        s = snap(et(2026, 6, 18, 9, 0), spy=None, qqq=-0.3, vix=None)
        back = MarketSnapshot.from_dict(s.to_dict())
        assert back.spy_day_return_pp is None
        assert back.qqq_day_return_pp == -0.3
        assert back.vix is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
