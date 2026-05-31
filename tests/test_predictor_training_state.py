"""Tests for load_predictor_training_state — the L4468 SSOT accessor.

Training state must read from the authoritative manifest (fresh every Saturday
training), NOT latest.json's weekday-inference mirror (stale all weekend). These
pin the manifest→normalized-keys mapping incl. the W1/L4469 leak-free fields.
"""
from unittest.mock import MagicMock, patch

import pandas as pd  # noqa: F401 — pre-import under real open() before mocks

_MOCK_CONFIG = {
    "s3": {"research_bucket": "test-bucket", "trades_bucket": "test-bucket"},
    "cache_ttl": {"signals": 900, "trades": 900, "research": 3600, "backtest": 3600},
    "paths": {
        "signals": "signals/{date}/signals.json",
        "trades_full": "trades/trades_full.csv",
        "eod_pnl": "trades/eod_pnl.csv",
        "scoring_weights": "config/scoring_weights.json",
        "scoring_weights_history_prefix": "scoring_weights_history/",
        "backtest_prefix": "backtest/",
        "research_db": "research.db",
    },
}


def _import_s3_loader():
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
            from loaders import s3_loader
            return s3_loader


_FAKE_MANIFEST = {
    "date": "2026-06-06",
    "promoted": True,
    "version": "v3.0-meta",
    "models": {
        "meta_model": {"ic": 0.499},
        "momentum": {"test_ic": 0.001},
        "volatility": {"test_ic": 0.328},
    },
    "walk_forward": {
        "momentum_median_ic": -0.001,
        "volatility_median_ic": 0.327,
    },
    "meta_model_oos_ic_leakfree": {"status": "ok", "xsec_ic": 0.061},
    "meta_model_oos_ic_cpcv": {"status": "ok", "mean_ic": 0.058},
    "meta_model_promotion_stats": {
        "overfit": {"dsr": 0.93},
        "downside": {"sortino_of_ic": 0.84, "cvar_of_ic": -0.12},
    },
}


def test_training_state_reads_authoritative_manifest_fields():
    mod = _import_s3_loader()
    with patch.object(mod, "load_predictor_manifest", return_value=_FAKE_MANIFEST):
        ts = mod.load_predictor_training_state()
    assert ts["last_trained"] == "2026-06-06"   # manifest date, not latest.json
    assert ts["promoted"] is True
    assert ts["meta_ic_in_sample"] == 0.499
    assert ts["volatility_median_ic"] == 0.327
    # W1 leak-free metrics surface from the manifest (the trustworthy lens)
    assert ts["oos_ic_leakfree"]["xsec_ic"] == 0.061
    assert ts["promotion_stats"]["downside"]["sortino_of_ic"] == 0.84


def test_training_state_empty_manifest_returns_empty():
    mod = _import_s3_loader()
    with patch.object(mod, "load_predictor_manifest", return_value={}):
        assert mod.load_predictor_training_state() == {}


def test_training_state_tolerates_missing_subsections():
    mod = _import_s3_loader()
    with patch.object(mod, "load_predictor_manifest",
                      return_value={"date": "2026-06-06", "promoted": False}):
        ts = mod.load_predictor_training_state()
    assert ts["last_trained"] == "2026-06-06"
    assert ts["promoted"] is False
    assert ts["meta_ic_in_sample"] is None        # no models block → None, no crash
    assert ts["oos_ic_leakfree"] is None
