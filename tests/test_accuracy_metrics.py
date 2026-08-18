"""Tests for shared/accuracy_metrics.py."""

import pandas as pd
import pytest

from shared.accuracy_metrics import (
    compute_drawdown,
    compute_sharpe,
    find_drawdown_episodes,
    wilson_ci,
)


class TestWilsonCI:
    def test_perfect(self):
        lo, hi = wilson_ci(100, 100)
        assert lo > 0.95
        assert hi == pytest.approx(1.0, abs=0.01)

    def test_zero_successes(self):
        lo, hi = wilson_ci(0, 100)
        assert lo == pytest.approx(0.0, abs=0.01)
        assert hi < 0.05

    def test_half(self):
        lo, hi = wilson_ci(50, 100)
        assert 0.40 < lo < 0.50
        assert 0.50 < hi < 0.60

    def test_zero_total(self):
        lo, hi = wilson_ci(0, 0)
        assert lo == 0.0
        assert hi == 0.0

    def test_small_sample(self):
        lo, hi = wilson_ci(3, 5)
        assert 0.0 < lo < 0.60
        assert hi > 0.60


class TestComputeDrawdown:
    def test_no_drawdown(self):
        daily = pd.Series([0.01, 0.02, 0.01])
        dd = compute_drawdown(daily)
        assert (dd <= 0).all()

    def test_drawdown_after_drop(self):
        daily = pd.Series([0.10, -0.15, 0.02])
        dd = compute_drawdown(daily)
        assert dd.iloc[1] < 0

    def test_recovery(self):
        daily = pd.Series([0.10, -0.05, 0.10, 0.10])
        dd = compute_drawdown(daily)
        assert dd.iloc[-1] == pytest.approx(0.0, abs=0.01)


class TestComputeSharpe:
    def test_constant_returns_are_undefined_not_a_float_artifact(self):
        """A flat series has zero volatility — the Sharpe is undefined.

        This asserted ``sharpe > 10`` on the strength of "constant returns =
        infinite Sharpe, but float precision". The number it was accepting was
        **7.3e+16**: pandas' Welford variance leaves a 2.2e-19 residue on a
        constant series, and dividing 0.001 by it produced an artifact that
        rendered on the dashboard as a Sharpe. Since config-I7597 the maths is
        `nousergon_lib.quant.riskstats.sharpe_ratio`, whose zero-volatility
        answer is ``None`` — undefined, never a measured value. That is the
        fleet convention (`crucible-backtester/vectorbt_bridge.py:360`).
        """
        daily = pd.Series([0.001] * 252)
        assert compute_sharpe(daily, min_rows=10) is None

    def test_real_positive_returns_score(self):
        daily = pd.Series([0.001, 0.002, 0.0005, 0.0015] * 63)
        sharpe = compute_sharpe(daily, min_rows=10)
        assert sharpe is not None
        assert sharpe > 10

    def test_insufficient_rows(self):
        daily = pd.Series([0.01, 0.02])
        assert compute_sharpe(daily, min_rows=30) is None

    def test_mixed_returns(self):
        daily = pd.Series([0.01, -0.01, 0.02, -0.005] * 20)
        sharpe = compute_sharpe(daily, min_rows=10)
        assert sharpe is not None
        assert isinstance(sharpe, float)


class TestFindDrawdownEpisodes:
    def test_single_episode_recovered(self):
        dd = pd.Series([0.0, -0.02, -0.05, -0.03, 0.0])
        dates = pd.Series(pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04", "2026-04-05"]))
        episodes = find_drawdown_episodes(dd, dates)
        assert len(episodes) == 1
        assert episodes[0]["Status"] == "Recovered"
        assert episodes[0]["Depth"] == "-5.00%"

    def test_active_drawdown(self):
        dd = pd.Series([0.0, -0.02, -0.05])
        dates = pd.Series(pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03"]))
        episodes = find_drawdown_episodes(dd, dates)
        assert len(episodes) == 1
        assert episodes[0]["Status"] == "Active"

    def test_no_drawdown(self):
        dd = pd.Series([0.0, 0.0, 0.0])
        dates = pd.Series(pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03"]))
        episodes = find_drawdown_episodes(dd, dates)
        assert len(episodes) == 0

    def test_multiple_episodes(self):
        dd = pd.Series([0.0, -0.02, 0.0, -0.03, -0.01, 0.0])
        dates = pd.Series(pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06"]))
        episodes = find_drawdown_episodes(dd, dates)
        assert len(episodes) == 2
        assert all(e["Status"] == "Recovered" for e in episodes)
