"""Unknown cost is preserved through every public usage view aggregation."""
import pandas as pd
from shared.usage_source_view import complete_cost, cost_label, source_breakdown, daily_cost_by_source


def test_unknown_cost_propagates_without_discarding_tokens():
    df = pd.DataFrame([
        {"date": "2026-11-19", "source": "groom", "cost_usd": 2.0, "total": 100, "wet": 50},
        {"date": "2026-11-19", "source": "groom", "cost_usd": None, "total": 200, "wet": 75},
    ])
    assert pd.isna(complete_cost(df.cost_usd))
    assert cost_label(complete_cost(df.cost_usd)) == "unpriced"
    row = source_breakdown(df).iloc[0]
    assert pd.isna(row.cost_usd) and row.total_tokens == 300 and row.wet == 125
    assert pd.isna(daily_cost_by_source(df).iloc[0]["groom"])
    assert complete_cost([1.25, 2]) == 3.25


def test_loader_preserves_unknown_model_and_hour_costs(monkeypatch):
    import importlib
    from datetime import date
    from unittest.mock import MagicMock
    loader = importlib.import_module("loaders.s3_loader")
    day = date.today().isoformat()
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": f"claude_code_usage/groom/{day}.json"}]}
    ]
    monkeypatch.setattr(loader, "get_s3_client", lambda: client)
    monkeypatch.setattr(loader, "_research_bucket", lambda: "fixture")
    monkeypatch.setattr(loader, "download_s3_json", lambda *args: {
        "by_model": {"opus": {"cost_usd": None, "total": 30, "wet": 20,
                              "unpriced_responses": 1}},
        "by_hour": {"12": {"opus": {"cost_usd": None, "wet": 20},
                            "sonnet": {"cost_usd": 2.0, "wet": 5}}},
    })
    models, hours = loader.load_claude_code_usage()
    assert pd.isna(models.iloc[0].cost_usd)
    assert models.iloc[0].total == 30 and models.iloc[0].unpriced_responses == 1
    assert pd.isna(hours.iloc[0].cost_usd) and hours.iloc[0].wet == 25
