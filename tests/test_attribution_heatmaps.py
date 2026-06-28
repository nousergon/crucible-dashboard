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
        # bps = alpha_contribution_pct * 100 × (today_nav / prior_nav)
        # prior trading day is 2026-04-13 (NAV 990); today 1000 → ratio 1000/990
        assert aaa20[COL_CONTRIB_BPS] == pytest.approx(0.5 * 100 * (1000 / 990))
        assert aaa20["weight"] == pytest.approx(0.1)

        bbb20 = df[(df.ticker == "BBB")].iloc[0]
        assert bbb20[COL_RELATIVE] == pytest.approx(-2.0)
        assert bbb20[COL_CONTRIB_BPS] == pytest.approx(-0.2 * 100 * (1000 / 990))

        aaa21 = df[(df.ticker == "AAA") & (df.date == "2026-04-21")].iloc[0]
        assert aaa21[COL_RELATIVE] == pytest.approx(1.5)  # 1.0 - (-0.5)
        # prior-NAV basis: prior day 2026-04-20 NAV 1000, today 1010 → 1010/1000
        assert aaa21[COL_CONTRIB_BPS] == pytest.approx(0.3 * 100 * (1010 / 1000))

    def test_contribution_prior_nav_basis_fallback(self):
        """First available day (no prior NAV) falls back to today's-NAV basis
        (ratio 1) — the contribution is unscaled."""
        eod = pd.DataFrame([
            {
                "date": "2026-04-20",
                "portfolio_nav": 1000.0,
                "spy_return_pct": 0.0,
                "positions_snapshot": _snap({
                    "AAA": {"daily_return_pct": 1.0,
                            "alpha_contribution_pct": 0.4,
                            "market_value": 100.0, "sector": "Tech"},
                }),
            },
        ])
        df = build_long_frame(eod)
        row = df[df.ticker == "AAA"].iloc[0]
        assert row[COL_CONTRIB_BPS] == pytest.approx(40.0)

    def test_empty_inputs(self):
        assert build_long_frame(None).empty
        assert build_long_frame(pd.DataFrame()).empty
        # right columns even when empty
        assert COL_RELATIVE in build_long_frame(None).columns


class TestPreAttributionReconstruction:
    """config#1212: pre-2026-04-20 rows carry only a per-position
    ``closing_price`` (no stored ``daily_return_pct`` / ``alpha_contribution_pct``).
    We reconstruct the price-relative lens read-side; the contribution lens is
    left blank (not reconstructable from price alone)."""

    def _pre_eod(self):
        """Two consecutive pre-attribution days for DDD with closing prices, and
        one isolated pre-attribution name EEE held a single day (no prior close
        → not reconstructable, must be skipped)."""
        return pd.DataFrame([
            {
                "date": "2026-03-09",
                "portfolio_nav": 900.0,
                "spy_return_pct": 0.5,
                "positions_snapshot": _snap({
                    "DDD": {"closing_price": 100.0, "market_value": 300.0,
                            "sector": "Tech"},
                }),
            },
            {
                "date": "2026-03-10",
                "portfolio_nav": 905.0,
                "spy_return_pct": 1.0,
                "positions_snapshot": _snap({
                    "DDD": {"closing_price": 102.0, "market_value": 306.0,
                            "sector": "Tech"},
                    # EEE first appears today: no prior close → skipped.
                    "EEE": {"closing_price": 50.0, "market_value": 50.0,
                            "sector": "Health"},
                }),
            },
        ])

    def test_reconstructs_price_relative_lens(self):
        df = build_long_frame(self._pre_eod())
        # DDD on 03-09 has no prior close → skipped; DDD on 03-10 reconstructs;
        # EEE on 03-10 has no prior close → skipped. So exactly one row.
        assert len(df) == 1
        row = df.iloc[0]
        assert row["ticker"] == "DDD"
        assert row["date"] == "2026-03-10"
        # day-over-day price return: 102/100 - 1 = +2.0%
        assert row[COL_RETURN] == pytest.approx(2.0)
        # market-relative = 2.0 - 1.0 (SPY), same formula as stored rows
        assert row[COL_RELATIVE] == pytest.approx(1.0)
        # contribution lens NOT reconstructable from price → blank
        assert pd.isna(row[COL_CONTRIB_BPS])
        # weight still derivable from market_value / nav
        assert row["weight"] == pytest.approx(306.0 / 905.0)

    def test_first_held_day_skipped_no_prior_close(self):
        """A pre-attribution position on its first held day (no prior close) and
        a position with neither stored return nor any close are both skipped."""
        eod = pd.DataFrame([
            {
                "date": "2026-03-09",
                "portfolio_nav": 900.0,
                "spy_return_pct": 0.0,
                "positions_snapshot": _snap({
                    # has a close but no prior → skipped
                    "DDD": {"closing_price": 100.0, "market_value": 100.0,
                            "sector": "Tech"},
                    # neither stored return nor any close → skipped
                    "FFF": {"market_value": 10.0, "sector": "X"},
                }),
            },
        ])
        assert build_long_frame(eod).empty

    def test_contribution_matrix_gaps_pre_then_post(self):
        """Mixed history: pre-attribution rows fill the RETURN/market-relative
        lenses but leave the contribution matrix with gaps; post rows keep full
        attribution. The contribution matrix only contains the post-4/20 cell."""
        eod = pd.DataFrame([
            {
                "date": "2026-03-09",
                "portfolio_nav": 900.0,
                "spy_return_pct": 0.0,
                "positions_snapshot": _snap({
                    "DDD": {"closing_price": 100.0, "market_value": 100.0,
                            "sector": "Tech"},
                }),
            },
            {
                "date": "2026-03-10",
                "portfolio_nav": 900.0,
                "spy_return_pct": 0.0,
                "positions_snapshot": _snap({
                    "DDD": {"closing_price": 110.0, "market_value": 110.0,
                            "sector": "Tech"},
                }),
            },
            {  # post-attribution: stored return + contribution
                "date": "2026-04-20",
                "portfolio_nav": 1000.0,
                "spy_return_pct": 0.0,
                "positions_snapshot": _snap({
                    "DDD": {"daily_return_pct": 1.0,
                            "alpha_contribution_pct": 0.5,
                            "market_value": 120.0, "sector": "Tech"},
                }),
            },
        ])
        df = build_long_frame(eod)
        # reconstructed 03-10 (+10%) and stored 04-20 (+1%); 03-09 skipped.
        assert set(df["date"]) == {"2026-03-10", "2026-04-20"}
        recon = df[df.date == "2026-03-10"].iloc[0]
        assert recon[COL_RETURN] == pytest.approx(10.0)
        assert pd.isna(recon[COL_CONTRIB_BPS])
        # RETURN matrix has both columns; contribution matrix only the post col.
        ret_m = to_matrix(df, COL_RETURN, period_col="date")
        assert list(ret_m.columns) == ["2026-03-10", "2026-04-20"]
        contrib_m = to_matrix(df, COL_CONTRIB_BPS, period_col="date")
        assert list(contrib_m.columns) == ["2026-04-20"]

    def test_post_attribution_rows_unchanged_with_close_present(self):
        """A post-attribution row that also happens to carry closing_price must
        still use its STORED daily_return_pct (not a reconstructed one)."""
        eod = pd.DataFrame([
            {
                "date": "2026-04-20",
                "portfolio_nav": 1000.0,
                "spy_return_pct": 0.0,
                "positions_snapshot": _snap({
                    "GGG": {"daily_return_pct": 3.0,
                            "alpha_contribution_pct": 0.1,
                            "closing_price": 999.0,  # would imply a different ret
                            "market_value": 100.0, "sector": "Tech"},
                }),
            },
        ])
        row = build_long_frame(eod).iloc[0]
        assert row[COL_RETURN] == pytest.approx(3.0)  # stored, not reconstructed
        assert row[COL_CONTRIB_BPS] == pytest.approx(0.1 * 100)  # ratio-1 first day


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
        # additive contribution on the prior-NAV basis:
        #   04-20: 0.5×100×(1000/990) + 04-21: 0.3×100×(1010/1000)
        assert aaa[COL_CONTRIB_BPS] == pytest.approx(
            0.5 * 100 * (1000 / 990) + 0.3 * 100 * (1010 / 1000)
        )

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
        assert m.loc["AAA", "2026-04-20"] == pytest.approx(
            0.5 * 100 * (1000 / 990) + 0.3 * 100 * (1010 / 1000)
        )

    def test_empty_metric_returns_empty(self):
        df = pd.DataFrame({"ticker": ["AAA"], "date": ["2026-04-20"],
                           COL_RELATIVE: [None]})
        assert to_matrix(df, COL_RELATIVE).empty
