"""Tests for intraday_live.py — the shared intraday-live derivation used by
BOTH the public live site and the private console, plus the shaded
make_intraday_curve chart builder.

Pure logic (DataFrame in, values out). The producer publishes raw marks;
this is where today's return/alpha + the intraday curve are derived against
the prior EOD close.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

pytest.importorskip("pandas")
import pandas as pd  # noqa: E402

from intraday_live import (  # noqa: E402
    LiveMetrics,
    build_intraday_curve,
    compute_live_metrics,
    series_date_for,
)


def _eod() -> pd.DataFrame:
    # date as strings on purpose — exercises the in-module coercion so the
    # console's raw load_eod_pnl() frame works without pre-parsing.
    return pd.DataFrame(
        {
            "date": ["2026-06-16", "2026-06-17"],
            "portfolio_nav": [1_010_630.61, 1_000_564.85],
            "spy_close": [750.33, 740.96],
        }
    )


_NOW = datetime(2026, 6, 18, 18, 38, 0, tzinfo=timezone.utc)


def _nav(**over) -> dict:
    base = {
        "timestamp": "2026-06-18T18:37:30.000Z",
        "ib_connected": True,
        "net_liquidation": 1_005_000.0,
        "spy_last": 745.0,
    }
    base.update(over)
    return base


class TestComputeLiveMetrics:
    def test_fresh_derives_return_and_alpha(self):
        m = compute_live_metrics(_nav(), _eod(), now_utc=_NOW)
        assert isinstance(m, LiveMetrics)
        assert m.nav == 1_005_000.0
        assert m.day_return == pytest.approx(1_005_000.0 / 1_000_564.85 - 1)
        assert m.spy_return == pytest.approx(745.0 / 740.96 - 1)
        assert m.day_alpha == pytest.approx(m.day_return - m.spy_return)
        assert m.as_of_et == "2:37 PM ET"

    def test_missing_spy_yields_return_no_alpha(self):
        m = compute_live_metrics(_nav(spy_last=None), _eod(), now_utc=_NOW)
        assert m.spy_return is None and m.day_alpha is None
        assert m.day_return == pytest.approx(1_005_000.0 / 1_000_564.85 - 1)

    def test_strictly_prior_baseline(self):
        nav = _nav(timestamp="2026-06-17T18:37:30.000Z")
        now = datetime(2026, 6, 17, 18, 38, 0, tzinfo=timezone.utc)
        m = compute_live_metrics(nav, _eod(), now_utc=now)
        assert m.day_return == pytest.approx(1_005_000.0 / 1_010_630.61 - 1)

    @pytest.mark.parametrize("nav,eod", [
        (None, _eod()),
        (_nav(), None),
        (_nav(ib_connected=False), _eod()),
        (_nav(net_liquidation=None), _eod()),
        (_nav(timestamp="2026-06-18T18:28:00.000Z"), _eod()),  # stale (10 min)
        (_nav(timestamp="2026-06-18T18:40:00.000Z"), _eod()),  # future
        (_nav(timestamp="not-a-date"), _eod()),
    ])
    def test_not_live_returns_none(self, nav, eod):
        assert compute_live_metrics(nav, eod, now_utc=_NOW) is None

    def test_no_prior_baseline(self):
        nav = _nav(timestamp="2026-06-16T18:37:30.000Z")
        now = datetime(2026, 6, 16, 18, 38, 0, tzinfo=timezone.utc)
        assert compute_live_metrics(nav, _eod(), now_utc=now) is None


class TestSeriesDateFor:
    def test_et_date(self):
        assert series_date_for({"timestamp": "2026-06-18T18:37:30.000Z"}) == "2026-06-18"

    def test_utc_evening_prior_et_day(self):
        assert series_date_for({"timestamp": "2026-06-19T02:30:00.000Z"}) == "2026-06-18"

    def test_none_unparseable(self):
        assert series_date_for(None) is None
        assert series_date_for({"timestamp": "nope"}) is None


_PTS = [
    {"t": "2026-06-18T13:45:00Z", "nav": 1_000_564.85, "spy": 740.96},  # = prior close
    {"t": "2026-06-18T14:45:00Z", "nav": 1_005_000.0, "spy": 745.0},
    {"t": "2026-06-18T15:45:00Z", "nav": 1_010_000.0, "spy": 744.0},
]


def _series(points, trading_day="2026-06-18") -> dict:
    return {"trading_day": trading_day, "points": points}


class TestBuildIntradayCurve:
    def test_rebases_to_prior_close(self):
        df = build_intraday_curve(_series(_PTS), _eod())
        assert list(df.columns) == ["time", "port_cum", "spy_cum"]
        assert df["port_cum"].iloc[0] == pytest.approx(0.0)
        assert df["spy_cum"].iloc[0] == pytest.approx(0.0)
        assert df["port_cum"].iloc[1] == pytest.approx((1_005_000.0 / 1_000_564.85 - 1) * 100)
        assert df["spy_cum"].iloc[2] == pytest.approx((744.0 / 740.96 - 1) * 100)

    def test_time_et_naive(self):
        df = build_intraday_curve(_series(_PTS), _eod())
        first = df["time"].iloc[0]
        assert first.hour == 9 and first.minute == 45 and first.tzinfo is None

    def test_no_spy_baseline_na_column(self):
        eod = _eod()
        eod.loc[:, "spy_close"] = float("nan")
        df = build_intraday_curve(_series(_PTS), eod)
        assert df["spy_cum"].isna().all()

    def test_skips_bad_points(self):
        pts = [
            {"t": "2026-06-18T13:45:00Z", "nav": 1_000_564.85, "spy": 740.96},
            {"t": "2026-06-18T14:45:00Z", "nav": None, "spy": 745.0},
            {"t": "bad", "nav": 1_005_000.0, "spy": 745.0},
            {"t": "2026-06-18T15:45:00Z", "nav": 1_010_000.0, "spy": 744.0},
        ]
        assert len(build_intraday_curve(_series(pts), _eod())) == 2

    def test_none_and_empty(self):
        assert build_intraday_curve(None, _eod()) is None
        assert build_intraday_curve(_series(_PTS), None) is None
        assert build_intraday_curve(_series([]), _eod()) is None

    def test_no_prior_close(self):
        assert build_intraday_curve(_series(_PTS, trading_day="2026-06-16"), _eod()) is None


class TestMakeIntradayCurveChart:
    """The plotly figure builds without error on synthetic data — the
    render-time path tests can't exercise via the (mocked) Streamlit page."""

    def test_console_chart_builds_with_shading(self):
        go = pytest.importorskip("plotly.graph_objects")
        from charts.nav_chart import make_intraday_curve

        df = build_intraday_curve(_series(_PTS), _eod())
        fig = make_intraday_curve(df)
        assert isinstance(fig, go.Figure)
        names = [t.name for t in fig.data]
        assert "Portfolio" in names and "S&P 500" in names
        # Shaded region present (toself fills) since SPY baseline exists.
        assert any(getattr(t, "fill", None) == "toself" for t in fig.data)

    def test_console_chart_no_spy_no_shading(self):
        pytest.importorskip("plotly.graph_objects")
        from charts.nav_chart import make_intraday_curve

        eod = _eod()
        eod.loc[:, "spy_close"] = float("nan")
        df = build_intraday_curve(_series(_PTS), eod)
        fig = make_intraday_curve(df)
        assert not any(getattr(t, "fill", None) == "toself" for t in fig.data)
        assert "S&P 500" not in [t.name for t in fig.data]

    def test_empty_curve_is_graceful(self):
        pytest.importorskip("plotly.graph_objects")
        from charts.nav_chart import make_intraday_curve

        fig = make_intraday_curve(pd.DataFrame(columns=["time", "port_cum", "spy_cum"]))
        assert fig is not None
