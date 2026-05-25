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


class TestImplausibleCostRowDefense:
    """Defensive consumer-side filter — mirror of producer-side guard in
    alpha-engine-research's scripts/aggregate_costs._is_plausible_cost_row.
    Catches historical pollution (2026-05-13 $1014 spike) until the
    producer-side cleanup of that day's parquet lands.
    """

    def test_drops_test_fixture_run_ids(self):
        mod = _import_s3_loader()
        # Mix of real + the exact 2026-05-13 pollution shape.
        rows = [
            {"run_id": "2026-05-13", "agent_id": "sector_team:tech",
             "cost_usd": 0.012, "input_tokens": 4000, "output_tokens": 1200},
            {"run_id": "run-x", "agent_id": "big_spender",
             "cost_usd": 1000.0, "input_tokens": 1_000_000_000, "output_tokens": 0},
            {"run_id": "run-budget-test", "agent_id": "runaway_agent",
             "cost_usd": 10.0, "input_tokens": 10_000_000, "output_tokens": 0},
            {"run_id": "run-1", "agent_id": "a",
             "cost_usd": 1.0, "input_tokens": 1_000_000, "output_tokens": 0},
        ]
        parquet = _make_cost_parquet_bytes(rows)
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-13"]):
            with patch.object(mod, "_s3_get_object", return_value=parquet):
                df = mod.load_llm_cost_parquets()
        # Only the real production row survives.
        assert len(df) == 1
        assert df.iloc[0]["agent_id"] == "sector_team:tech"
        assert df["cost_usd"].sum() == pytest.approx(0.012)

    def test_drops_implausibly_high_token_count(self):
        mod = _import_s3_loader()
        # Real-looking run_id but absurd token count — catches a
        # fixture that uses an ISO-date run_id but fabricated counts.
        rows = [
            {"run_id": "2026-05-13", "agent_id": "real_agent",
             "cost_usd": 0.01, "input_tokens": 4000, "output_tokens": 1200},
            {"run_id": "2026-05-13", "agent_id": "fake_agent",
             "cost_usd": 500.0, "input_tokens": 50_000_000, "output_tokens": 0},
        ]
        parquet = _make_cost_parquet_bytes(rows)
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-13"]):
            with patch.object(mod, "_s3_get_object", return_value=parquet):
                df = mod.load_llm_cost_parquets()
        assert len(df) == 1
        assert df.iloc[0]["agent_id"] == "real_agent"

    def test_passthrough_when_run_id_column_missing(self):
        mod = _import_s3_loader()
        # Pre-instrumentation parquets don't have run_id — must not be
        # blanket-dropped (would break the existing test fixtures and
        # any historical archive without the column).
        rows = [{"agent_id": "ic_cio", "cost_usd": 0.05}]
        parquet = _make_cost_parquet_bytes(rows)
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-02"]):
            with patch.object(mod, "_s3_get_object", return_value=parquet):
                df = mod.load_llm_cost_parquets()
        assert len(df) == 1
        assert df.iloc[0]["cost_usd"] == 0.05

    def test_real_iso_date_run_id_passes(self):
        mod = _import_s3_loader()
        # Pin that the regex allows the production run_id formats actually
        # seen in 2026-05-15 and 2026-05-17 captures.
        rows = [
            {"run_id": "2026-05-13", "agent_id": "sector_team:tech",
             "cost_usd": 0.01, "input_tokens": 4000, "output_tokens": 1200},
            {"run_id": "2026-05-15", "agent_id": "sector_team:financials",
             "cost_usd": 0.02, "input_tokens": 5000, "output_tokens": 1500},
        ]
        parquet = _make_cost_parquet_bytes(rows)
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-15"]):
            with patch.object(mod, "_s3_get_object", return_value=parquet):
                df = mod.load_llm_cost_parquets()
        assert len(df) == 2


class TestSectorTeamColumnPreservation:
    """Page 23's "By sector team" tab (L1141 deliverable c) pivots on
    ``sector_team_id``. The loader must preserve the column end-to-end
    through the parquet roundtrip + capture-date tag + implausibility
    filter, including the NaN values that cross-sector agents
    (``macro_economist``, ``ic_cio``) carry by design.
    """

    def test_sector_team_id_preserved_for_team_agents(self):
        mod = _import_s3_loader()
        rows = [
            {"run_id": "2026-05-15", "agent_id": "sector_team:tech",
             "sector_team_id": "tech", "cost_usd": 0.10,
             "input_tokens": 4000, "output_tokens": 1200},
            {"run_id": "2026-05-15", "agent_id": "sector_team:financials",
             "sector_team_id": "financials", "cost_usd": 0.20,
             "input_tokens": 5000, "output_tokens": 1300},
        ]
        parquet = _make_cost_parquet_bytes(rows)
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-15"]):
            with patch.object(mod, "_s3_get_object", return_value=parquet):
                df = mod.load_llm_cost_parquets()
        assert "sector_team_id" in df.columns
        assert set(df["sector_team_id"]) == {"tech", "financials"}

    def test_sector_team_id_preserves_none_for_cross_sector_agents(self):
        # macro_economist + ic_cio carry sector_team_id=None upstream; the
        # page surfaces them under "(none)" via fillna. The loader must
        # not coerce them to something that breaks downstream fillna.
        mod = _import_s3_loader()
        rows = [
            {"run_id": "2026-05-15", "agent_id": "sector_team:tech",
             "sector_team_id": "tech", "cost_usd": 0.10,
             "input_tokens": 4000, "output_tokens": 1200},
            {"run_id": "2026-05-15", "agent_id": "macro_economist",
             "sector_team_id": None, "cost_usd": 0.05,
             "input_tokens": 1000, "output_tokens": 200},
            {"run_id": "2026-05-15", "agent_id": "ic_cio",
             "sector_team_id": None, "cost_usd": 0.07,
             "input_tokens": 1200, "output_tokens": 300},
        ]
        parquet = _make_cost_parquet_bytes(rows)
        with patch.object(mod, "list_s3_prefixes", return_value=["2026-05-15"]):
            with patch.object(mod, "_s3_get_object", return_value=parquet):
                df = mod.load_llm_cost_parquets()
        # Three rows survived; two of them have null sector_team_id.
        assert len(df) == 3
        assert df["sector_team_id"].isna().sum() == 2
        # The page's fillna("(none)") should produce the expected key.
        filled = df["sector_team_id"].fillna("(none)").astype(str)
        assert set(filled) == {"tech", "(none)"}
