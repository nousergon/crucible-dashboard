"""Tests for components.sweep_distribution (config#1444 item 3)."""

from __future__ import annotations

from components.sweep_distribution import selected_percentile, sweep_summary


class TestSelectedPercentile:
    def test_top_is_100(self):
        assert selected_percentile([0.1, 0.2, 0.3, 0.4], 0.4) == 100.0

    def test_middle(self):
        assert selected_percentile([1, 2, 3, 4], 2) == 50.0

    def test_none_inputs(self):
        assert selected_percentile([], 1.0) is None
        assert selected_percentile([1, 2], None) is None


class TestSweepSummary:
    def test_summary_fields(self):
        s = sweep_summary([0.1, 0.5, 0.9, 0.3, 0.7])
        assert s["n"] == 5
        assert s["min"] == 0.1
        assert s["max"] == 0.9
        assert s["selected"] == 0.9               # defaults to best
        assert s["selected_percentile"] == 100.0

    def test_explicit_selected(self):
        s = sweep_summary([0.1, 0.2, 0.3, 0.4], selected=0.2)
        assert s["selected"] == 0.2
        assert s["selected_percentile"] == 50.0

    def test_drops_nan_inf_nonnumeric(self):
        s = sweep_summary([0.1, float("nan"), float("inf"), "x", 0.3])
        assert s["n"] == 2

    def test_empty(self):
        assert sweep_summary([]) == {"n": 0}
