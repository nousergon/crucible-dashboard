"""Unit tests for the fleet trading calendar this repo imports.

alpha-engine-config-I10142: this repo used to carry its own hand-maintained
``trading_calendar.py`` fork (2025-2030 only, no lower-edge guard) that
answered ``True`` for pre-2025 weekday holidays -- Labor Day 2024, July 4
2024, Thanksgiving 2024, Christmas 2024 -- because it never imported
``krepis``/``nousergon_lib`` at all. The fork is deleted; every caller
(``sla_status.py``, ``live/morning_brief.py``,
``loaders/fleet_status_loader.py``) now imports
``nousergon_lib.trading_calendar`` directly, so these tests exercise the
real fleet module rather than a local copy.
"""
from datetime import date

from nousergon_lib.trading_calendar import (
    NYSE_CALENDAR_COVERS_FROM,
    NYSE_HOLIDAYS,
    TradingCalendarPrecedesCoverageError,
    is_trading_day,
    next_trading_day,
)


class TestIsTradingDay:
    def test_weekday_non_holiday(self):
        # 2026-04-01 is a Wednesday, not a holiday
        assert is_trading_day(date(2026, 4, 1)) is True

    def test_saturday(self):
        assert is_trading_day(date(2026, 4, 4)) is False

    def test_sunday(self):
        assert is_trading_day(date(2026, 4, 5)) is False

    def test_good_friday_2026(self):
        # 2026-04-03 is Good Friday
        assert is_trading_day(date(2026, 4, 3)) is False

    def test_christmas_2026(self):
        assert is_trading_day(date(2026, 12, 25)) is False

    def test_mlk_day_2026(self):
        assert is_trading_day(date(2026, 1, 19)) is False

    def test_normal_monday(self):
        # 2026-04-06 is a normal Monday
        assert is_trading_day(date(2026, 4, 6)) is True


class TestPre2025DefectFixed:
    """I10142 / krepis-PR206: the old fork's table started at 2025 with no
    below-range guard, so every one of these read as a trading day (True).
    The fleet module covers back to 2016 and must answer False."""

    def test_labor_day_2024(self):
        assert is_trading_day(date(2024, 9, 2)) is False

    def test_independence_day_2024(self):
        assert is_trading_day(date(2024, 7, 4)) is False

    def test_thanksgiving_2024(self):
        assert is_trading_day(date(2024, 11, 28)) is False

    def test_christmas_2024(self):
        assert is_trading_day(date(2024, 12, 25)) is False


class TestPrecedesCoverageRaises:
    """Migration hazard named in I10142: below NYSE_CALENDAR_COVERS_FROM
    (2016-01-01) the fleet module RAISES rather than silently answering
    True, unlike the old fork. Every caller in this repo (sla_status.py's
    <=17-day lookback, fleet_status_loader.py's 12-day lookback, and
    morning_brief.py's today-only check) is bounded well inside the
    covered range, so this is a coverage-edge property test, not a
    reachable path from any current caller."""

    def test_raises_below_coverage_floor(self):
        import pytest

        before = date(NYSE_CALENDAR_COVERS_FROM.year - 1, 12, 31)
        with pytest.raises(TradingCalendarPrecedesCoverageError):
            is_trading_day(before)


class TestNextTradingDay:
    def test_next_after_friday(self):
        # 2026-04-03 is Good Friday, so next trading day is Monday 2026-04-06
        result = next_trading_day(date(2026, 4, 3))
        assert result == date(2026, 4, 6)

    def test_next_after_wednesday(self):
        # 2026-04-01 (Wed) -> next is 2026-04-02 (Thu)
        result = next_trading_day(date(2026, 4, 1))
        assert result == date(2026, 4, 2)

    def test_next_after_saturday(self):
        # Saturday -> Monday (if not a holiday)
        result = next_trading_day(date(2026, 4, 4))
        assert result == date(2026, 4, 6)

    def test_skips_holiday_weekend_cluster(self):
        # Thanksgiving 2026: Thu Nov 26. Wed->Fri (skip Thu holiday)
        result = next_trading_day(date(2026, 11, 25))
        assert result == date(2026, 11, 27)


class TestHolidayCompleteness:
    def test_all_years_have_holidays(self):
        """Each year 2025-2030 should have holidays defined."""
        for year in range(2025, 2031):
            year_holidays = [h for h in NYSE_HOLIDAYS if h.year == year]
            assert len(year_holidays) >= 9, f"Year {year} has only {len(year_holidays)} holidays"

    def test_no_weekday_holidays_on_weekends(self):
        """Observed holidays should fall on weekdays (exchanges observe on Mon/Fri)."""
        for h in NYSE_HOLIDAYS:
            assert h.weekday() <= 4, f"Holiday {h} falls on a weekend (day {h.weekday()})"
