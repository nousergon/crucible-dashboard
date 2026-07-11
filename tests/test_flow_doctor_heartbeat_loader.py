"""Tests for the flow-doctor heartbeat loaders (config#646).

The System Health "Flow-Doctor Heartbeat" page reads each producing flow's
end-of-run ``emit_heartbeat()`` snapshot from
``s3://alpha-engine-research/_flow_doctor/heartbeat/{flow}/{date}.json``. These
pin the two loaders (flow discovery + newest-date load) against a mocked S3.
"""
from unittest.mock import MagicMock, patch

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


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages


def test_list_flows_reads_common_prefixes():
    mod = _import_s3_loader()
    pages = [{
        "CommonPrefixes": [
            {"Prefix": "_flow_doctor/heartbeat/predictor/"},
            {"Prefix": "_flow_doctor/heartbeat/research-alerts/"},
            {"Prefix": "_flow_doctor/heartbeat/daemon/"},
        ]
    }]
    client = MagicMock()
    client.get_paginator.return_value = _FakePaginator(pages)
    with patch.object(mod, "get_s3_client", return_value=client):
        flows = mod.list_flow_doctor_heartbeat_flows()
    assert flows == ["daemon", "predictor", "research-alerts"]  # sorted


def test_list_flows_empty_on_error():
    mod = _import_s3_loader()
    with patch.object(mod, "get_s3_client", side_effect=RuntimeError("boom")):
        assert mod.list_flow_doctor_heartbeat_flows() == []


def test_load_latest_picks_newest_date():
    mod = _import_s3_loader()
    pages = [{
        "Contents": [
            {"Key": "_flow_doctor/heartbeat/predictor/2026-07-09.json"},
            {"Key": "_flow_doctor/heartbeat/predictor/2026-07-11.json"},
            {"Key": "_flow_doctor/heartbeat/predictor/2026-07-10.json"},
            {"Key": "_flow_doctor/heartbeat/predictor/latest.json"},  # ignored (not ISO)
        ]
    }]
    client = MagicMock()
    client.get_paginator.return_value = _FakePaginator(pages)
    payload = {"flow_name": "predictor", "status": {"healthy": True}}
    with patch.object(mod, "get_s3_client", return_value=client), \
         patch.object(mod, "_fetch_s3_json", return_value=payload) as fetch:
        out = mod.load_flow_doctor_heartbeat_latest("predictor")
    assert out == payload
    # Must fetch the newest ISO-dated key, not latest.json.
    fetched_key = fetch.call_args[0][1]
    assert fetched_key == "_flow_doctor/heartbeat/predictor/2026-07-11.json"


def test_load_latest_none_when_no_heartbeat():
    mod = _import_s3_loader()
    client = MagicMock()
    client.get_paginator.return_value = _FakePaginator([{"Contents": []}])
    with patch.object(mod, "get_s3_client", return_value=client):
        assert mod.load_flow_doctor_heartbeat_latest("predictor") is None
