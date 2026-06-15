"""Tests for load_optimizer_risk_history in loaders/s3_loader.py.

Mirrors the fresh-import + mocked-S3 pattern of tests/test_news_articles_loader.py.
Consumer side of the cross-repo optimizer_risk_history contract: tolerate the
empty/absent case, skip the latest.json sidecar, and pass dated records through.
"""

from unittest.mock import MagicMock, patch

_MOCK_CONFIG = {
    "s3": {"research_bucket": "test-bucket", "trades_bucket": "test-bucket"},
    "cache_ttl": {"signals": 900, "trades": 900, "research": 3600, "backtest": 3600},
    "paths": {"signals": "signals/{date}/signals.json", "research_db": "research.db"},
}


def _import_s3_loader():
    import sys
    if "loaders.s3_loader" in sys.modules:
        del sys.modules["loaders.s3_loader"]
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value=_MOCK_CONFIG):
            from loaders import s3_loader
            return s3_loader


def _client_for(keys):
    client = MagicMock()
    client.list_objects_v2.return_value = {"Contents": [{"Key": k} for k in keys]}
    return client


class TestLoadOptimizerRiskHistory:
    def test_reads_dated_records_skipping_latest_sidecar(self):
        mod = _import_s3_loader()
        keys = [
            "config/optimizer_risk_history/2606130900.json",
            "config/optimizer_risk_history/2606200900.json",
            "config/optimizer_risk_history/latest.json",  # must be skipped
        ]
        recs = {
            "config/optimizer_risk_history/2606130900.json": {"trading_day": "2026-06-13", "risk_aversion": 5.0},
            "config/optimizer_risk_history/2606200900.json": {"trading_day": "2026-06-20", "risk_aversion": 0.238},
        }
        with patch.object(mod, "get_s3_client", return_value=_client_for(keys)):
            with patch.object(mod, "_fetch_s3_json", side_effect=lambda b, k: recs.get(k)):
                out = mod.load_optimizer_risk_history()
        assert [r["trading_day"] for r in out] == ["2026-06-13", "2026-06-20"]
        assert all("latest" not in str(r) for r in out)

    def test_empty_when_no_objects(self):
        mod = _import_s3_loader()
        with patch.object(mod, "get_s3_client", return_value=_client_for([])):
            assert mod.load_optimizer_risk_history() == []

    def test_empty_on_list_error(self):
        mod = _import_s3_loader()
        client = MagicMock()
        client.list_objects_v2.side_effect = RuntimeError("boom")
        with patch.object(mod, "get_s3_client", return_value=client):
            with patch.object(mod, "_record_s3_error", MagicMock()):
                assert mod.load_optimizer_risk_history() == []

    def test_skips_non_dict_payloads(self):
        mod = _import_s3_loader()
        keys = ["config/optimizer_risk_history/2606130900.json"]
        with patch.object(mod, "get_s3_client", return_value=_client_for(keys)):
            with patch.object(mod, "_fetch_s3_json", return_value=None):
                assert mod.load_optimizer_risk_history() == []
