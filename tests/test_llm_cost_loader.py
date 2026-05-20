"""Tests for `load_llm_cost_parquets` in loaders/s3_loader.py.

Patterns mirror tests/test_s3_loader_core.py (mocked S3 client + mocked
streamlit at the conftest layer).
"""

import io
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

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
    """Force a fresh import of loaders.s3_loader regardless of prior test
    pollution. test_db_loader.py installs a MagicMock under
    ``sys.modules['loaders.s3_loader']`` at collection time which the
    other s3-loader test files clear via ``del sys.modules[...]`` per the
    pattern at tests/test_s3_loader.py:46-47.
    """
    import sys
    if "loaders.s3_loader" in sys.modules:
        del sys.modules["loaders.s3_loader"]
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
            from loaders import s3_loader
            return s3_loader


def _make_cost_parquet_bytes(rows: list[dict]) -> bytes:
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


class TestLoadLlmCostParquets:
    def test_returns_empty_when_no_date_prefixes(self):
        mod = _import_s3_loader()
        with patch.object(mod, "list_s3_prefixes", return_value=[]):
            df = mod.load_llm_cost_parquets()
        assert df.empty

    def test_concats_recent_partitions_with_capture_date_tag(self):
        mod = _import_s3_loader()
        rows_a = [{"agent_id": "ic_cio", "cost_usd": 0.10, "model_name": "claude-sonnet-4-6"}]
        rows_b = [{"agent_id": "macro_economist", "cost_usd": 0.04, "model_name": "claude-sonnet-4-6"}]
        parquet_a = _make_cost_parquet_bytes(rows_a)
        parquet_b = _make_cost_parquet_bytes(rows_b)

        def fake_get_object(_bucket, key):
            if "2026-05-16" in key:
                return parquet_a
            if "2026-05-17" in key:
                return parquet_b
            return None

        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-16", "2026-05-17"]):
            with patch.object(mod, "_s3_get_object", side_effect=fake_get_object):
                df = mod.load_llm_cost_parquets()

        assert len(df) == 2
        assert set(df["capture_date"]) == {"2026-05-16", "2026-05-17"}
        assert df["cost_usd"].sum() == pytest.approx(0.14)

    def test_n_recent_caps_at_tail(self):
        # 5 dates available, n_recent=2 → only the last 2 are loaded.
        mod = _import_s3_loader()
        parquet = _make_cost_parquet_bytes([{"agent_id": "a", "cost_usd": 1.0}])
        dates = ["2026-05-02", "2026-05-09", "2026-05-13", "2026-05-16", "2026-05-17"]
        loaded_keys: list[str] = []

        def fake_get_object(_bucket, key):
            loaded_keys.append(key)
            return parquet

        with patch.object(mod, "list_s3_prefixes", return_value=dates):
            with patch.object(mod, "_s3_get_object", side_effect=fake_get_object):
                df = mod.load_llm_cost_parquets(n_recent=2)

        assert len(loaded_keys) == 2
        assert "2026-05-16" in loaded_keys[0]
        assert "2026-05-17" in loaded_keys[1]
        assert set(df["capture_date"]) == {"2026-05-16", "2026-05-17"}

    def test_skips_missing_parquet_bodies(self):
        mod = _import_s3_loader()
        parquet = _make_cost_parquet_bytes([{"agent_id": "a", "cost_usd": 0.5}])

        def fake_get_object(_bucket, key):
            # second date returns None (missing)
            return parquet if "2026-05-16" in key else None

        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-16", "2026-05-17"]):
            with patch.object(mod, "_s3_get_object", side_effect=fake_get_object):
                df = mod.load_llm_cost_parquets()

        assert len(df) == 1
        assert df["capture_date"].iloc[0] == "2026-05-16"

    def test_returns_empty_when_every_parquet_fails(self):
        mod = _import_s3_loader()
        # bytes that aren't valid parquet
        bogus = b"not parquet"
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-16"]):
            with patch.object(mod, "_s3_get_object", return_value=bogus):
                df = mod.load_llm_cost_parquets()
        assert df.empty
