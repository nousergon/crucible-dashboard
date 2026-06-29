"""Cross-repo consumer-contract test for the Universe Board page.

Pins the dashboard's contract with crucible-research ``scoring/universe_board.py``
(artifact ``scanner/universe/latest.json``, ``schema_version=2``): the page's
flatten transform MUST correctly consume the producer's exact field names
(``attractiveness_score``/``attractiveness_raw``, ``pillars.<pillar>``,
``pillar_contributions``, ``metrics.<metric>``, ``gate.quant_filter_pass``,
``gate_stage``, ``gate_trace``, top-level ``pillar_weights``/``gate_config``).
A producer/consumer drift here would silently blank columns on the board.

The fixture below mirrors a record EXACTLY as the producer emits it (see
crucible-research ``tests/test_universe_board.py``). ``loaders/universe_board.py``
is pure pandas (no Streamlit) so this runs without mocking the UI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from loaders.universe_board import (  # noqa: E402
    PILLARS,
    board_meta,
    contributions_df,
    flatten_board,
    gate_trace_df,
    index_by_ticker,
)


def _producer_board() -> dict:
    """A schema_version=2 board as crucible-research scoring/universe_board.py emits it."""
    return {
        "schema_version": 2,
        "as_of": "2026-06-28",
        "universe_count": 2,
        "attractiveness_method": "sector_neutral_zscore_percentile",
        "pillars": list(PILLARS),
        "pillar_weights": {p: round(1 / 6, 6) for p in PILLARS},
        "gate_config": {"min_avg_volume": 500_000, "min_price": 0.0, "tech_score_min": 60,
                        "max_atr_pct": 8.0, "momentum_top_n": 60},
        "stocks": [
            {
                "ticker": "AAPL",
                "sector": "Information Technology",
                "country": "United States",
                "industry": "Consumer Electronics",
                "attractiveness_score": 100.0,
                "attractiveness_raw": 0.1667,
                "pillars": {"quality": 90.0, "value": 30.0, "momentum": 85.0,
                            "growth": 80.0, "stewardship": 70.0, "defensiveness": 60.0},
                "pillar_contributions": {"quality": 0.1667, "value": -0.1667, "momentum": 0.1667,
                                         "growth": 0.0, "stewardship": 0.0, "defensiveness": 0.0},
                "pillar_coverage": {"quality": 4, "momentum": 5},
                "focus_score": 80.0, "focus_stance": "momentum", "tech_score": 72.0,
                "gate": {"quant_filter_pass": 1, "filter_fail_reason": None},
                "gate_stage": "passed",
                "gate_trace": [
                    {"stage": "liquidity", "metric": "avg_volume_20d", "value": 55_000_000.0,
                     "threshold": 500_000, "op": ">=", "pass": True},
                    {"stage": "volatility", "metric": "atr_pct", "value": 1.5,
                     "threshold": 8.0, "op": "<=", "pass": True},
                    {"stage": "tech_score", "metric": "tech_score", "value": 72.0,
                     "threshold": 60, "op": ">=", "pass": True},
                ],
                "metrics": {
                    "current_price": 195.0, "market_cap": 3.0e12, "pe": 30.0, "pb": 40.0,
                    "fcf_yield": 0.04, "dividend_yield": 0.005, "debt_to_equity": 1.5,
                    "current_ratio": 1.2, "payout_ratio": 0.15, "roe": 1.5, "gross_margin": 0.44,
                    "revenue_growth_3y": 0.08, "eps_growth_3y": 0.10, "rsi_14": 58.0,
                    "momentum_20d": 0.03, "return_60d": 0.08, "return_120d": 0.12,
                    "realized_vol_20d": 0.22, "atr_pct": 0.015, "dist_from_52w_high": -0.04,
                    "price_vs_ma200": 0.10, "beta": 1.2, "avg_volume": 55_000_000.0,
                },
            },
            {
                # Rejected, Ireland-domiciled, partial pillar coverage, sparse metrics.
                "ticker": "LIN",
                "sector": "Materials",
                "country": "Ireland",
                "industry": "Specialty Chemicals",
                "attractiveness_score": 50.0,
                "attractiveness_raw": -0.25,
                "pillars": {"quality": 50.0, "value": 40.0, "momentum": 30.0,
                            "growth": None, "stewardship": None, "defensiveness": 60.0},
                "pillar_contributions": {"quality": -0.25, "value": 0.25, "momentum": -0.25,
                                         "defensiveness": 0.0},
                "pillar_coverage": {"quality": 4},
                "focus_score": 55.0, "focus_stance": "quality", "tech_score": 40.0,
                "gate": {"quant_filter_pass": 0, "filter_fail_reason": "liquidity"},
                "gate_stage": "liquidity",
                "gate_trace": [
                    {"stage": "liquidity", "metric": "avg_volume_20d", "value": 120_000.0,
                     "threshold": 500_000, "op": ">=", "pass": False},
                    {"stage": "volatility", "metric": "atr_pct", "value": 1.2,
                     "threshold": 8.0, "op": "<=", "pass": True},
                    {"stage": "tech_score", "metric": "tech_score", "value": 40.0,
                     "threshold": 60, "op": ">=", "pass": False},
                ],
                "metrics": {"current_price": 460.0, "pe": 36.0},
            },
        ],
    }


def _v1_board() -> dict:
    """A legacy schema_version=1 artifact (no v2 fields) — must still flatten."""
    return {
        "schema_version": 1,
        "as_of": "2026-06-20",
        "attractiveness_method": "equal_weight_available_pillars",
        "pillars": list(PILLARS),
        "stocks": [{
            "ticker": "AAPL", "sector": "Information Technology", "country": "United States",
            "industry": "Consumer Electronics", "attractiveness_score": 69.17,
            "pillars": {p: 70.0 for p in PILLARS},
            "focus_score": 80.0, "focus_stance": "momentum", "tech_score": 72.0,
            "gate": {"quant_filter_pass": 1, "filter_fail_reason": None},
            "metrics": {"current_price": 195.0, "pe": 30.0},
        }],
    }


def test_flatten_consumes_producer_fields():
    df = flatten_board(_producer_board())
    assert len(df) == 2
    aapl = df.set_index("ticker").loc["AAPL"]
    # Attractiveness (percentile + raw) + a pillar + a denormalized metric + gate all land.
    assert aapl["attractiveness"] == 100.0
    assert aapl["attractiveness_raw"] == 0.1667
    assert aapl["gate_stage"] == "passed"
    assert aapl["quality"] == 90.0
    assert aapl["pe"] == 30.0
    assert aapl["country"] == "United States"
    assert aapl["gate"] == "PASS"
    assert aapl["mkt_cap"] == 3.0e12


def test_board_meta_surfaces_weights_and_gate_config():
    meta = board_meta(_producer_board())
    assert meta["schema_version"] == 2
    assert meta["attractiveness_method"] == "sector_neutral_zscore_percentile"
    assert round(sum(meta["pillar_weights"].values()), 3) == 1.0
    assert meta["gate_config"]["min_avg_volume"] == 500_000


def test_contributions_df_sums_to_raw_and_orders_by_pillar():
    by_t = index_by_ticker(_producer_board())
    cdf = contributions_df(by_t["AAPL"])
    assert list(cdf["pillar"]) == PILLARS  # full coverage, pillar order
    assert round(cdf["contribution"].sum(), 3) == 0.167  # == attractiveness_raw
    # LIN only has its 4 available pillars.
    lin = contributions_df(by_t["LIN"])
    assert set(lin["pillar"]) == {"quality", "value", "momentum", "defensiveness"}


def test_gate_trace_df_shows_value_vs_threshold():
    by_t = index_by_ticker(_producer_board())
    lin = gate_trace_df(by_t["LIN"])
    liq = lin.set_index("stage").loc["liquidity"]
    assert liq["value"] == 120_000.0 and liq["threshold"] == 500_000 and liq["result"] == "FAIL"
    aapl = gate_trace_df(by_t["AAPL"]).set_index("stage")
    assert aapl.loc["liquidity"]["result"] == "pass"


def test_v1_artifact_still_flattens():
    df = flatten_board(_v1_board())
    aapl = df.set_index("ticker").loc["AAPL"]
    assert aapl["attractiveness"] == 69.17
    assert pd.isna(aapl["attractiveness_raw"])  # v1 has no raw → NaN
    # v1 detail helpers degrade to empty, not error.
    by_t = index_by_ticker(_v1_board())
    assert contributions_df(by_t["AAPL"]).empty
    assert gate_trace_df(by_t["AAPL"]).empty


def test_partial_coverage_and_missing_metrics_degrade_to_nan():
    df = flatten_board(_producer_board()).set_index("ticker")
    lin = df.loc["LIN"]
    assert lin["gate"] == "FAIL"
    assert lin["fail_reason"] == "liquidity"
    assert lin["country"] == "Ireland"
    # Null pillar + absent metric → NaN (a coverage gap, never fabricated).
    assert lin["growth"] != lin["growth"]   # NaN
    assert lin["roe"] != lin["roe"]         # NaN (metric absent from LIN)


def test_every_pillar_column_present():
    df = flatten_board(_producer_board())
    for p in PILLARS:
        assert p in df.columns


def test_empty_board_yields_empty_frame():
    assert flatten_board({"stocks": []}).empty
    assert flatten_board(None).empty


def test_loader_reads_pinned_latest_key():
    """The loader must read the producer's exact artifact key. A drift here
    silently shows an empty board."""
    src = (REPO_ROOT / "loaders" / "s3_loader.py").read_text()
    assert "scanner/universe/latest.json" in src
    assert "scanner/universe/{date_str}/universe.json" in src


def test_page_registered_in_nav():
    # Hosted under the "Universe & Scanner" front page (lazy view-host) post-IA-
    # reorg rather than registered directly in app.py.
    host_src = (REPO_ROOT / "views" / "host_universe_scanner.py").read_text()
    assert "39_Universe_Board.py" in host_src
    app_src = (REPO_ROOT / "app.py").read_text()
    assert 'page("host_universe_scanner.py"' in app_src
    assert (REPO_ROOT / "views" / "39_Universe_Board.py").exists()
