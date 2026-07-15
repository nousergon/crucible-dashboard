"""Unit tests for trading_calendar — NYSE holiday and trading day checks."""
from datetime import date

from trading_calendar import is_trading_day, next_trading_day, NYSE_HOLIDAYS


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
