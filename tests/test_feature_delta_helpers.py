"""Tests for production-vs-research feature delta helpers in loaders/utils.py.

Pure functions; no S3 or streamlit dependency.
"""

import pandas as pd

from loaders.utils import production_feature_set, research_feature_set


class TestProductionFeatureSet:
    def test_flattens_l2_and_l1(self):
        feature_list = {
            "trained_at": "2026-05-16",
            "version": "v3.0-meta",
            "l2_features": ["a", "b", "c"],
            "l1_features": {
                "momentum": ["x", "y"],
                "volatility": ["z"],
            },
        }
        assert production_feature_set(feature_list) == {"a", "b", "c", "x", "y", "z"}

    def test_l2_l1_overlap_collapses(self):
        # research_calibrator's L1 features overlap with L2's research_* features
        # in the real production payload — set semantics must collapse them.
        feature_list = {
            "l2_features": ["research_composite_score", "momentum_score"],
            "l1_features": {
                "research_calibrator": ["research_composite_score"],
            },
        }
        result = production_feature_set(feature_list)
        assert result == {"research_composite_score", "momentum_score"}

    def test_handles_none_input(self):
        assert production_feature_set(None) == set()

    def test_handles_non_dict_input(self):
        assert production_feature_set(["not", "a", "dict"]) == set()  # type: ignore[arg-type]

    def test_handles_missing_keys(self):
        assert production_feature_set({}) == set()
        assert production_feature_set({"l2_features": None, "l1_features": None}) == set()

    def test_handles_non_list_l1_values(self):
        # If l1_features has a malformed (non-list) value, skip it without raising.
        feature_list = {
            "l2_features": ["a"],
            "l1_features": {"momentum": ["m1"], "bogus": "not-a-list"},
        }
        assert production_feature_set(feature_list) == {"a", "m1"}


class TestResearchFeatureSet:
    def test_unions_columns_excluding_ticker_and_date(self):
        df1 = pd.DataFrame({"ticker": ["A"], "date": ["2026-05-20"], "rsi_14": [50.0]})
        df2 = pd.DataFrame({"ticker": ["A"], "date": ["2026-05-20"], "atr_14_pct": [0.02]})
        assert research_feature_set(df1, df2) == {"rsi_14", "atr_14_pct"}

    def test_skips_none_dataframes(self):
        df = pd.DataFrame({"ticker": ["A"], "rsi_14": [50.0]})
        assert research_feature_set(None, df, None) == {"rsi_14"}

    def test_all_none(self):
        assert research_feature_set(None, None) == set()

    def test_no_dfs(self):
        assert research_feature_set() == set()

    def test_substrate_delta_computation(self):
        # End-to-end shape: store has 4 features, production consumes 2.
        # Delta (store - production) is the substrate slack.
        df = pd.DataFrame({"ticker": ["A"], "date": ["2026-05-20"], "rsi_14": [1], "atr_14_pct": [1], "vol_ratio_10_60": [1], "iv_rank": [1]})
        feature_list = {"l2_features": [], "l1_features": {"momentum": ["rsi_14"], "volatility": ["atr_14_pct"]}}
        prod = production_feature_set(feature_list)
        research = research_feature_set(df)
        assert research - prod == {"vol_ratio_10_60", "iv_rank"}
        assert prod - research == set()
