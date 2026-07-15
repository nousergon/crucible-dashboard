"""
tests/test_s3_loader_data.py — Unit tests for S3 data loading functions.

Tests the actual data loading pipeline (download_s3_json, download_s3_csv,
load_predictions_json, etc.) with mocked S3 responses.
Complements test_s3_loader.py which only tests error tracking.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock streamlit before importing loaders
mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

_MOCK_CONFIG = {
    "s3": {"research_bucket": "test-research", "trades_bucket": "test-trades"},
    "cache_ttl": {"signals": 900, "trades": 900, "research": 3600},
    "paths": {
        "signals": "signals/{date}/signals.json",
        "trades_full": "trades/trades_full.csv",
        "eod_pnl": "trades/eod_pnl.csv",
        "scoring_weights": "config/scoring_weights.json",
        "scoring_weights_history_prefix": "config/scoring_weights_history/",
        "backtest_prefix": "backtest/",
    },
}


def _get_loader():
    """Import (or reimport) s3_loader with mocked config."""
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
            with patch("os.path.getmtime", return_value=1.0):
                if "loaders.s3_loader" in sys.modules:
                    del sys.modules["loaders.s3_loader"]
                from loaders import s3_loader
                s3_loader._config_cache = None
                s3_loader._config_mtime = 0.0
                s3_loader._recent_s3_errors.clear()
                with patch("builtins.open", MagicMock()):
                    with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
                        s3_loader.load_config()
    return s3_loader


class TestFetchS3Json:
    """Tests for _fetch_s3_json (core JSON fetch helper)."""

    def test_valid_json(self):
        loader = _get_loader()
        payload = {"key": "value", "count": 42}
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(payload).encode()):
            result = loader._fetch_s3_json("bucket", "key.json")
        assert result == payload

    def test_missing_key_returns_none(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader._fetch_s3_json("bucket", "missing.json")
        assert result is None

    def test_invalid_json_returns_none(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=b"not json{{{"):
            result = loader._fetch_s3_json("bucket", "bad.json")
        assert result is None
        assert len(loader.get_recent_s3_errors()) == 1
        assert loader.get_recent_s3_errors()[0]["error_type"] == "JSONParseError"


class TestDownloadS3Csv:
    """Tests for download_s3_csv."""

    def test_valid_csv(self):
        loader = _get_loader()
        csv_content = b"ticker,price\nAAPL,150.0\nMSFT,300.0\n"
        with patch.object(loader, "_s3_get_object", return_value=csv_content):
            result = loader.download_s3_csv("bucket", "data.csv")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2
        assert list(result.columns) == ["ticker", "price"]

    def test_missing_csv_returns_none(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader.download_s3_csv("bucket", "missing.csv")
        assert result is None

    def test_malformed_csv_returns_none(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=b"\x00\x01\x02\x03"):
            result = loader.download_s3_csv("bucket", "bad.csv")
        # Pandas may parse binary as single-column or fail; either way should not crash
        assert result is None or isinstance(result, pd.DataFrame)


class TestDownloadS3Text:
    """Tests for download_s3_text."""

    def test_valid_text(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=b"Hello, World!"):
            result = loader.download_s3_text("bucket", "file.txt")
        assert result == "Hello, World!"

    def test_missing_text_returns_none(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader.download_s3_text("bucket", "missing.txt")
        assert result is None


class TestLoadPredictionsJson:
    """Tests for load_predictions_json."""

    def test_valid_predictions(self):
        loader = _get_loader()
        payload = {
            "predictions": [
                {"ticker": "AAPL", "predicted_direction": "UP", "prediction_confidence": 0.8},
                {"ticker": "MSFT", "predicted_direction": "DOWN", "prediction_confidence": 0.7},
            ]
        }
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(payload).encode()):
            result = loader.load_predictions_json("2024-01-15")

        assert isinstance(result, dict)
        assert "AAPL" in result
        assert "MSFT" in result
        assert result["AAPL"]["predicted_direction"] == "UP"

    def test_empty_predictions(self):
        loader = _get_loader()
        payload = {"predictions": []}
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(payload).encode()):
            result = loader.load_predictions_json("2024-01-15")
        assert result == {}

    def test_missing_file_returns_empty(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader.load_predictions_json("2024-01-15")
        assert result == {}

    def test_no_date_uses_latest(self):
        loader = _get_loader()
        payload = {"predictions": [{"ticker": "SPY", "predicted_direction": "FLAT"}]}
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(payload).encode()) as mock_get:
            result = loader.load_predictions_json()
        assert "SPY" in result


class TestLoadPredictorMetrics:
    """Tests for load_predictor_metrics."""

    def test_valid_metrics(self):
        loader = _get_loader()
        metrics = {"ic": 0.15, "accuracy": 0.62, "model_date": "2024-01-15"}
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(metrics).encode()):
            result = loader.load_predictor_metrics()
        assert result["ic"] == 0.15

    def test_missing_returns_empty_dict(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader.load_predictor_metrics()
        assert result == {}


class TestLoadModelZooLeaderboard:
    """Tests for load_model_zoo_leaderboard (L4544/L4571 model-zoo panel)."""

    def test_valid_leaderboard(self):
        loader = _get_loader()
        board = {
            "date": "2026-06-13", "mode": "cutover",
            "champion": {"forward_days": 21, "cpcv_mean_ic": 0.058},
            "margin": 0.01,
            "candidates": [
                {"spec_id": "champion-arch", "version_id": "base-v",
                 "forward_days": 21, "cpcv_mean_ic": 0.09, "passes_gate": True,
                 "eligible": True, "reason": "eligible"},
            ],
            "winner_version_id": "base-v", "promoted": "base-v",
        }
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(board).encode()):
            result = loader.load_model_zoo_leaderboard("2026-06-13")
        assert result["mode"] == "cutover"
        assert result["promoted"] == "base-v"
        assert result["candidates"][0]["spec_id"] == "champion-arch"

    def test_missing_returns_empty_dict(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader.load_model_zoo_leaderboard()
        assert result == {}

    def test_list_dates_delegates_to_prefix_listing(self):
        loader = _get_loader()
        with patch.object(loader, "list_s3_prefixes",
                          return_value=["2026-06-13", "2026-06-20"]) as m:
            dates = loader.list_model_zoo_leaderboard_dates()
        assert dates == ["2026-06-13", "2026-06-20"]
        assert m.call_args[0][1] == "predictor/model_zoo/leaderboard/"


class TestLoadModeHistory:
    """Tests for load_mode_history."""

    def test_valid_list(self):
        loader = _get_loader()
        history = [{"date": "2024-01-15", "mode": "ensemble"}]
        with patch.object(loader, "_s3_get_object", return_value=json.dumps(history).encode()):
            result = loader.load_mode_history()
        assert len(result) == 1

    def test_dict_response_returns_empty_list(self):
        """If S3 returns a dict instead of list, should return []."""
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=json.dumps({"not": "a list"}).encode()):
            result = loader.load_mode_history()
        assert result == []

    def test_missing_returns_empty_list(self):
        loader = _get_loader()
        with patch.object(loader, "_s3_get_object", return_value=None):
            result = loader.load_mode_history()
        assert result == []


class TestConfigEnvOverride:
    """Tests for DASHBOARD_CONFIG_PATH env override."""

    def test_uses_env_var_when_set(self):
        loader = _get_loader()
        loader._config_cache = None
        loader._config_mtime = 0.0

        with patch.dict("os.environ", {"DASHBOARD_CONFIG_PATH": "/custom/config.yaml"}):
            with patch("builtins.open", MagicMock()) as mock_open:
                with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
                    with patch("os.path.getmtime", return_value=2.0):
                        loader.load_config()

            mock_open.assert_called_with("/custom/config.yaml")
