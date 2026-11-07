"""Recorded TTL/WET metadata survives loading and labels mixed windows."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tests.test_llm_cost_loader import _import_s3_loader
from shared.usage_regimes import usage_regime_breaks, usage_regime_caption


def _load(documents):
    mod = _import_s3_loader()
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{
        "Contents": [{"Key": key} for key in documents]
    }]
    with patch.object(mod, "get_s3_client", return_value=client), patch.object(
        mod, "download_s3_json", side_effect=lambda bucket, key: documents[key]
    ):
        return mod.load_claude_code_usage(n_days=100000)


def test_loader_preserves_regimes_and_values_on_both_frames():
    documents = {}
    for date, metadata in [
        ("2026-09-18", {}),
        ("2026-09-19", {"cache_ttl": "5m", "wet_version": 2}),
        ("2026-09-22", {"cache_ttl": "1h", "wet_version": 3}),
    ]:
        documents[f"claude_code_usage/interactive/{date}.json"] = {
            **metadata, "by_model": {"opus": {"wet": 7, "cost_usd": 0.25}},
            "by_hour": {"12": {"opus": {"wet": 7, "cost_usd": 0.25}}},
        }
    for frame in _load(documents):
        assert frame["cache_ttl"].tolist() == ["unrecorded", "5m", "1h"]
        assert frame["wet_version"].tolist() == ["unrecorded", 2, 3]
        assert frame["wet"].tolist() == [7, 7, 7]
        assert frame["cost_usd"].tolist() == [0.25, 0.25, 0.25]
        assert usage_regime_breaks(frame) == ["2026-09-19", "2026-09-22"]
        caption = usage_regime_caption(frame)
        assert "TTL unknown" in caption
        assert "2026-09-19 WET v2 dedupe fix" in caption
        assert "2026-09-22 WET v3 / cache-TTL change" in caption


@pytest.mark.parametrize("ttl", ["2h", "unrecorded", None, 5])
def test_unknown_explicit_ttl_fails_before_returning_totals(ttl):
    with pytest.raises(ValueError, match="Unknown cache_ttl"):
        _load({"claude_code_usage/groom/2026-09-22/run.json": {
            "cache_ttl": ttl, "by_model": {"opus": {"cost_usd": 99}},
        }})


def test_single_regime_and_empty_window():
    frame = pd.DataFrame([{"date": "2026-09-22", "cache_ttl": "1h", "wet_version": 3}])
    assert usage_regime_breaks(frame) == []
    assert usage_regime_caption(frame) == "Recorded pricing: cache TTL 1h / WET 3."
    assert usage_regime_breaks(pd.DataFrame()) == []


def test_wet_only_and_overlapping_regimes_are_captioned():
    frame = pd.DataFrame([
        {"date": "2026-09-22", "cache_ttl": "5m", "wet_version": 2},
        {"date": "2026-09-22", "cache_ttl": "5m", "wet_version": 3},
    ])
    assert "overlapping regimes" in usage_regime_caption(frame)
