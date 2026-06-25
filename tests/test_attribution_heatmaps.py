"""Tests for the Attribution Heatmaps data-shaping (``shared.attribution``).

``shared.attribution`` has no Streamlit dependency — it is pure pandas
transforms over the executor's ``eod_pnl`` history, so these run without any
mock. Covers: long-frame explosion (with market-relative + bps derivation and
the pre-attribution skip), geometric weekly compounding, additive weekly
contribution, and the (ticker × period) pivot.
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.attribution import (  # noqa: E402
    COL_CONTRIB_BPS,
    COL_RELATIVE,
    COL_RETURN,
    build_long_frame,
    build_weekly_frame,
    to_matrix,
)


def _snap(d: dict) -> str:
    return json.dumps(d)


@pytest.fixture
def eod_pnl():
    """Two days in the same ISO week (Mon 2026-04-20, Tue 2026-04-21) plus a
    prior-week row with no per-ticker returns (must be skipped) and a NaN
    snapshot row (must be tolerated)."""
    return pd.DataFrame([
        {
            "date": "2026-04-20",
            "portfolio_nav": 1000.0,
            "spy_return_pct": 1.0,
            "positions_snapshot": _snap({
                "AAA": {"daily_return_pct": 2.0, "alpha_contribution_pct": 0.5,
                        "market_value": 100.0, "sector": "Tech"},
                "BBB": {"daily_return_pct": -1.0, "alpha_contribution_pct": -0.2,
                        "market_value": 200.0, "sector": "Health"},
            }),
        },
        {
            "date": "2026-04-21",
            "portfolio_nav": 1010.0,
            "spy_return_pct": -0.5,
            "positions_snapshot": _snap({
                "AAA": {"daily_return_pct": 1.0, "alpha_contribution_pct": 0.3,
                        "market_value": 101.0, "sector": "Tech"},
            }),
        },
        {  # pre-attribution era — snapshot present but no per-ticker returns
            "date": "2026-04-13",
            "portfolio_nav": 990.0,
            "spy_return_pct": 0.2,
            "positions_snapshot": _snap({
                "CCC": {"market_value": 50.0, "sector": "X"},
            }),
        },
        {  # malformed / missing snapshot — tolerated, skipped
            "date": "2026-04-22",
            "portfolio_nav": 1000.0,
            "spy_return_pct": 0.0,
            "positions_snapshot": float("nan"),
        },
    ])


class TestBuildLongFrame:
    def test_explodes_and_derives(self, eod_pnl):
        df = build_long_frame(eod_pnl)
        # 3 rows: AAA×2 days + BBB×1 day. CCC (no return) + NaN row skipped.
        assert len(df) == 3
        assert set(df["ticker"]) == {"AAA", "BBB"}
        assert "CCC" not in set(df["ticker"])

        aaa20 = df[(df.ticker == "AAA") & (df.date == "2026-04-20")].iloc[0]
        assert aaa20[COL_RETURN] == pytest.approx(2.0)
        # market-relative = 2.0 - 1.0 (SPY)
        assert aaa20[COL_RELATIVE] == pytest.approx(1.0)
        # bps = alpha_contribution_pct * 100
        assert aaa20[COL_CONTRIB_BPS] == pytest.approx(50.0)
        assert aaa20["weight"] == pytest.approx(0.1)

        bbb20 = df[(df.ticker == "BBB")].iloc[0]
        assert bbb20[COL_RELATIVE] == pytest.approx(-2.0)
        assert bbb20[COL_CONTRIB_BPS] == pytest.approx(-20.0)

        aaa21 = df[(df.ticker == "AAA") & (df.date == "2026-04-21")].iloc[0]
        assert aaa21[COL_RELATIVE] == pytest.approx(1.5)  # 1.0 - (-0.5)

    def test_empty_inputs(self):
        assert build_long_frame(None).empty
        assert build_long_frame(pd.DataFrame()).empty
        # right columns even when empty
        assert COL_RELATIVE in build_long_frame(None).columns


class TestBuildWeeklyFrame:
    def test_geometric_and_additive_rollup(self, eod_pnl):
        wk = build_weekly_frame(build_long_frame(eod_pnl))
        # AAA held both days, BBB one → 2 weekly rows, same ISO-week Monday.
        assert set(wk["week"]) == {"2026-04-20"}
        aaa = wk[wk.ticker == "AAA"].iloc[0]
        assert aaa["n_days"] == 2
        # geometric total return: (1.02 * 1.01 - 1) * 100
        assert aaa[COL_RETURN] == pytest.approx((1.02 * 1.01 - 1) * 100)
        # geometric market-relative: compound(pos) - compound(SPY), same days
        expected_rel = (1.02 * 1.01 - 1.01 * 0.995) * 100
        assert aaa[COL_RELATIVE] == pytest.approx(expected_rel)
        # additive contribution: 50 + 30 bps
        assert aaa[COL_CONTRIB_BPS] == pytest.approx(80.0)

        bbb = wk[wk.ticker == "BBB"].iloc[0]
        assert bbb["n_days"] == 1
        assert bbb[COL_RETURN] == pytest.approx(-1.0)

    def test_empty(self):
        assert build_weekly_frame(pd.DataFrame()).empty


class TestToMatrix:
    def test_pivot_shape_order_and_gaps(self, eod_pnl):
        long_df = build_long_frame(eod_pnl)
        m = to_matrix(long_df, COL_RETURN, period_col="date")
        # columns chronological
        assert list(m.columns) == ["2026-04-20", "2026-04-21"]
        # AAA (net +3) ranks above BBB (net -1)
        assert list(m.index) == ["AAA", "BBB"]
        # BBB not held on 2026-04-21 → NaN gap
        assert pd.isna(m.loc["BBB", "2026-04-21"])
        assert m.loc["AAA", "2026-04-20"] == pytest.approx(2.0)

    def test_weekly_period_col(self, eod_pnl):
        wk = build_weekly_frame(build_long_frame(eod_pnl))
        m = to_matrix(wk, COL_CONTRIB_BPS, period_col="week")
        assert list(m.columns) == ["2026-04-20"]
        assert m.loc["AAA", "2026-04-20"] == pytest.approx(80.0)

    def test_empty_metric_returns_empty(self):
        df = pd.DataFrame({"ticker": ["AAA"], "date": ["2026-04-20"],
                           COL_RELATIVE: [None]})
        assert to_matrix(df, COL_RELATIVE).empty
