"""Tests for loaders/signal_loader.py — flattening and counting functions.

These test the pure data-transformation functions (no S3/Streamlit dependencies).
"""

import pandas as pd
import pytest

from loaders.signal_loader import (
    _cio_output,
    _extract_sub_scores,
    entrant_detail_df,
    entrant_flow_row,
    get_buy_candidates_df,
    get_sector_ratings_df,
    get_signal_counts,
    population_tickers,
    signals_to_df,
    ADVANCE_DECISIONS,
)


# ---------------------------------------------------------------------------
# _extract_sub_scores
# ---------------------------------------------------------------------------


class TestExtractSubScores:
    def test_nested_sub_scores(self):
        entry = {"sub_scores": {"technical": 80, "news": 60, "research": 70}}
        t, n, r = _extract_sub_scores(entry)
        assert t == 80
        assert n == 60
        assert r == 70

    def test_flat_keys(self):
        entry = {"technical": 75, "news": 65, "research": 55}
        t, n, r = _extract_sub_scores(entry)
        assert t == 75
        assert n == 65
        assert r == 55

    def test_empty_sub_scores_dict(self):
        entry = {"sub_scores": {}, "technical": 90}
        t, n, r = _extract_sub_scores(entry)
        assert t == 90

    def test_missing_all(self):
        entry = {"score": 80}
        t, n, r = _extract_sub_scores(entry)
        assert t is None
        assert n is None
        assert r is None


# ---------------------------------------------------------------------------
# signals_to_df
# ---------------------------------------------------------------------------


class TestSignalsToDf:
    def _make_signals(self, universe):
        return {"date": "2026-04-08", "universe": universe}

    def test_basic(self):
        universe = [
            {"ticker": "AAPL", "score": 82, "signal": "ENTER", "sector": "Technology"},
            {"ticker": "MSFT", "score": 75, "signal": "HOLD", "sector": "Technology"},
        ]
        df = signals_to_df(self._make_signals(universe))
        assert len(df) == 2
        assert "ticker" in df.columns
        assert df.iloc[0]["ticker"] == "AAPL"
        assert df.iloc[0]["score"] == 82

    def test_empty_universe(self):
        df = signals_to_df({"date": "2026-04-08", "universe": []})
        assert df.empty

    def test_none_input(self):
        df = signals_to_df(None)
        assert df.empty

    def test_no_universe_key(self):
        df = signals_to_df({"date": "2026-04-08"})
        assert df.empty

    def test_sub_scores_extracted(self):
        universe = [{"ticker": "GOOG", "sub_scores": {"technical": 85, "news": 70, "research": 75}}]
        df = signals_to_df(self._make_signals(universe))
        assert df.iloc[0]["technical"] == 85
        assert df.iloc[0]["news"] == 70

    def test_numeric_coercion(self):
        universe = [{"ticker": "AAPL", "score": "82.5", "price_target_upside": "0.15"}]
        df = signals_to_df(self._make_signals(universe))
        assert df.iloc[0]["score"] == pytest.approx(82.5)
        assert df.iloc[0]["price_target_upside"] == pytest.approx(0.15)

    def test_stale_default_false(self):
        universe = [{"ticker": "AAPL"}]
        df = signals_to_df(self._make_signals(universe))
        assert df.iloc[0]["stale"] == False


# ---------------------------------------------------------------------------
# get_buy_candidates_df
# ---------------------------------------------------------------------------


class TestGetBuyCandidatesDf:
    def test_basic(self):
        data = {"universe": [{"ticker": "NVDA", "score": 90, "signal": "ENTER"}]}
        df = get_buy_candidates_df(data)
        assert len(df) == 1
        assert df.iloc[0]["ticker"] == "NVDA"

    def test_none(self):
        assert get_buy_candidates_df(None).empty

    def test_empty(self):
        assert get_buy_candidates_df({"universe": []}).empty


# ---------------------------------------------------------------------------
# get_sector_ratings_df
# ---------------------------------------------------------------------------


class TestGetSectorRatingsDf:
    def test_dict_with_nested_values(self):
        data = {
            "sector_ratings": {
                "Technology": {"rating": "overweight", "rationale": "AI tailwinds"},
                "Healthcare": {"rating": "market_weight", "rationale": "Stable"},
            }
        }
        df = get_sector_ratings_df(data)
        assert len(df) == 2
        assert "sector" in df.columns
        assert "rating" in df.columns

    def test_dict_with_string_values(self):
        data = {"sector_ratings": {"Technology": "overweight", "Healthcare": "underweight"}}
        df = get_sector_ratings_df(data)
        assert len(df) == 2
        assert df.iloc[0]["rating"] == "overweight"

    def test_list_format(self):
        data = {"sector_ratings": [{"sector": "Tech", "rating": "OW"}]}
        df = get_sector_ratings_df(data)
        assert len(df) == 1

    def test_none(self):
        assert get_sector_ratings_df(None).empty

    def test_empty_dict(self):
        assert get_sector_ratings_df({"sector_ratings": {}}).empty


# ---------------------------------------------------------------------------
# get_signal_counts
# ---------------------------------------------------------------------------


class TestGetSignalCounts:
    def test_basic(self):
        data = {
            "universe": [
                {"ticker": "A", "signal": "ENTER"},
                {"ticker": "B", "signal": "ENTER"},
                {"ticker": "C", "signal": "HOLD"},
                {"ticker": "D", "signal": "EXIT"},
            ]
        }
        counts = get_signal_counts(data)
        assert counts["ENTER"] == 2
        assert counts["HOLD"] == 1
        assert counts["EXIT"] == 1
        assert counts["REDUCE"] == 0

    def test_none(self):
        counts = get_signal_counts(None)
        assert counts == {"ENTER": 0, "EXIT": 0, "REDUCE": 0, "HOLD": 0}

    def test_no_signals(self):
        counts = get_signal_counts({"universe": [{"ticker": "A"}]})
        assert counts["ENTER"] == 0


# ---------------------------------------------------------------------------
# Population flow / new-entrant tracking
# ---------------------------------------------------------------------------


class TestCioOutput:
    def test_unwraps_envelope(self):
        raw = {"run_date": "2026-06-05", "agent_id": "ic_cio",
               "output": {"ic_decisions": [], "advanced_tickers": []}}
        assert _cio_output(raw) == {"ic_decisions": [], "advanced_tickers": []}

    def test_accepts_bare_output(self):
        bare = {"ic_decisions": [{"ticker": "X", "decision": "ADVANCE"}]}
        assert _cio_output(bare) is bare

    def test_none_and_malformed(self):
        assert _cio_output(None) is None
        assert _cio_output({"run_date": "x"}) is None  # no ic_decisions anywhere
        assert _cio_output("not a dict") is None


class TestPopulationTickers:
    def test_string_entries(self):
        assert population_tickers({"population": ["AAPL", "MSFT"]}) == {"AAPL", "MSFT"}

    def test_dict_entries(self):
        data = {"population": [{"ticker": "AAPL"}, {"ticker": "MSFT"}]}
        assert population_tickers(data) == {"AAPL", "MSFT"}

    def test_empty_and_none(self):
        assert population_tickers(None) == set()
        assert population_tickers({}) == set()


class TestAdvanceDecisions:
    def test_includes_forced(self):
        # ADVANCE_FORCED must count as an entrant — the floor-enforcement bug class.
        assert "ADVANCE" in ADVANCE_DECISIONS
        assert "ADVANCE_FORCED" in ADVANCE_DECISIONS
        assert "REJECT" not in ADVANCE_DECISIONS


class TestEntrantFlowRow:
    def _cio(self):
        # 2 incumbents re-advanced, 1 fresh advanced, 2 fresh rejected.
        return {"ic_decisions": [
            {"ticker": "HELD1", "decision": "ADVANCE", "conviction": 80},
            {"ticker": "HELD2", "decision": "ADVANCE", "conviction": 75},
            {"ticker": "NEWA", "decision": "ADVANCE", "conviction": 64},
            {"ticker": "NEWR1", "decision": "REJECT", "conviction": 40},
            {"ticker": "NEWR2", "decision": "REJECT", "conviction": 35},
        ]}

    def test_net_new_counts_only_fresh_advances(self):
        prior = {"HELD1", "HELD2"}
        cur = {"HELD1", "HELD2", "NEWA"}
        row = entrant_flow_row("2026-06-05", self._cio(), prior, cur, have_prior=True)
        assert row["net_new_entrants"] == 1          # NEWA only — incumbents excluded
        assert row["new_candidates"] == 3            # NEWA, NEWR1, NEWR2
        assert row["new_rejected"] == 2
        assert row["candidates_total"] == 5
        assert row["new_conv_max"] == 64
        assert row["advanced_new_tickers"] == ["NEWA"]
        assert row["population_size"] == 3

    def test_zero_new_when_all_advances_are_incumbents(self):
        # Reproduces the 2026-06-05 case: every advance is already held.
        prior = {"HELD1", "HELD2"}
        cur = {"HELD1", "HELD2"}
        cio = {"ic_decisions": [
            {"ticker": "HELD1", "decision": "ADVANCE", "conviction": 80},
            {"ticker": "HELD2", "decision": "ADVANCE", "conviction": 75},
            {"ticker": "FRESH", "decision": "REJECT", "conviction": 40},
        ]}
        row = entrant_flow_row("2026-06-05", cio, prior, cur, have_prior=True)
        assert row["net_new_entrants"] == 0
        assert row["new_candidates"] == 1            # FRESH
        assert row["new_conv_max"] == 40

    def test_advance_forced_counts_as_entrant(self):
        prior = {"HELD1"}
        cur = {"HELD1", "FORCED"}
        cio = {"ic_decisions": [
            {"ticker": "HELD1", "decision": "ADVANCE", "conviction": 80},
            {"ticker": "FORCED", "decision": "ADVANCE_FORCED", "conviction": 45},
        ]}
        row = entrant_flow_row("2026-06-05", cio, prior, cur, have_prior=True)
        assert row["net_new_entrants"] == 1
        assert row["advanced_new_tickers"] == ["FORCED"]

    def test_missing_prior_baseline_yields_none(self):
        row = entrant_flow_row("2026-06-05", self._cio(), set(), {"A"}, have_prior=False)
        assert row["net_new_entrants"] is None
        assert row["new_candidates"] is None
        assert row["candidates_total"] == 5          # still report raw candidate count

    def test_none_cio_returns_none(self):
        assert entrant_flow_row("d", None, set(), set(), have_prior=True) is None


class TestEntrantDetailDf:
    def test_only_fresh_with_context_sorted(self):
        cio = {"ic_decisions": [
            {"ticker": "HELD", "decision": "ADVANCE", "conviction": 80,
             "rationale": "incumbent"},
            {"ticker": "GMED", "decision": "REJECT", "conviction": 40,
             "rationale": "merger risk"},
            {"ticker": "CART", "decision": "REJECT", "conviction": 35,
             "rationale": "anemic growth"},
        ]}
        sector_map = {"GMED": "Healthcare", "CART": "Consumer Discretionary"}
        sector_ratings = {"Healthcare": {"rating": "overweight"},
                          "Consumer Discretionary": {"rating": "underweight"}}
        df = entrant_detail_df(cio, {"HELD"}, sector_map, sector_ratings, have_prior=True)
        assert list(df["ticker"]) == ["GMED", "CART"]    # incumbent excluded, conv desc
        assert df.iloc[0]["sector_rating"] == "overweight"
        assert df.iloc[1]["sector_rating"] == "underweight"
        assert df.iloc[0]["decision"] == "❌ Rejected"

    def test_advanced_label_and_forced(self):
        cio = {"ic_decisions": [
            {"ticker": "NEWA", "decision": "ADVANCE", "conviction": 64},
            {"ticker": "FORCED", "decision": "ADVANCE_FORCED", "conviction": 45},
        ]}
        df = entrant_detail_df(cio, set(), {}, {}, have_prior=True)
        assert set(df["decision"]) == {"✅ Advanced"}

    def test_empty_cio(self):
        assert entrant_detail_df(None, set(), {}, {}, have_prior=True).empty

    def test_prefers_sector_from_decision_l4533(self):
        # Rejected fresh name isn't in the universe sector_map, but research
        # L4533 now persists sector on the decision — that must win.
        cio = {"ic_decisions": [
            {"ticker": "CART", "decision": "REJECT", "conviction": 35,
             "sector": "Consumer Discretionary", "rationale": "weak"},
        ]}
        sector_ratings = {"Consumer Discretionary": {"rating": "underweight"}}
        df = entrant_detail_df(cio, set(), {}, sector_ratings, have_prior=True)
        assert df.iloc[0]["sector"] == "Consumer Discretionary"
        assert df.iloc[0]["sector_rating"] == "underweight"

    def test_drops_all_empty_sector_columns(self):
        # No sector_map → sector/sector_rating all-None → columns dropped,
        # but reason (which carries the sector rationale) is retained.
        cio = {"ic_decisions": [
            {"ticker": "CART", "decision": "REJECT", "conviction": 35,
             "rationale": "Consumer Discretionary is underweight"},
        ]}
        df = entrant_detail_df(cio, set(), {}, {}, have_prior=True)
        assert "sector" not in df.columns
        assert "sector_rating" not in df.columns
        assert "reason" in df.columns
        assert "underweight" in df.iloc[0]["reason"]
