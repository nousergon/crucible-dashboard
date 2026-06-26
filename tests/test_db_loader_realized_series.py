"""Tests for the per-spec rolling realized-α series (config#1079).

Covers _per_spec_realized_series (per-date mean realized alpha + rolling mean,
NaN-safe) and get_per_spec_realized_alpha_series (union live champion + shadow
challengers, sorted, graceful degradation). Observability-only noise monitor.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock streamlit + s3_loader before importing db_loader (mirrors
# test_db_loader_scorecard). setdefault leaves any real module already loaded
# by an earlier test untouched, so this does not pollute other suites.
sys.modules.setdefault("streamlit", MagicMock())
sys.modules.setdefault("yaml", __import__("yaml") if "yaml" in sys.modules else MagicMock())
_mock_s3 = MagicMock()
_mock_s3.load_config.return_value = {
    "s3": {"research_bucket": "test-bucket"}, "paths": {"research_db": "research.db"},
}
sys.modules.setdefault("loaders.s3_loader", _mock_s3)

from loaders import db_loader  # noqa: E402
from loaders.db_loader import (  # noqa: E402
    _per_spec_realized_series,
    get_per_spec_realized_alpha_series,
)


def _frame(rows):
    return pd.DataFrame(rows, columns=[
        "model_version", "prediction_date", "actual_log_alpha",
    ])


def test_series_empty():
    assert _per_spec_realized_series(pd.DataFrame(), "champion", 8).empty


def test_series_all_null_alpha_is_empty():
    rows = [("V1", "2026-01-01", None), ("V1", "2026-01-02", None)]
    assert _per_spec_realized_series(_frame(rows), "champion", 8).empty


def test_series_per_date_mean_and_rolling():
    # One spec, three dates. Per-date mean alpha = [0.10, 0.20, 0.30].
    # Rolling mean (window=2, min_periods=1) = [0.10, 0.15, 0.25].
    rows = [
        ("V1", "2026-01-01", 0.05), ("V1", "2026-01-01", 0.15),  # mean 0.10
        ("V1", "2026-01-02", 0.20),                              # mean 0.20
        ("V1", "2026-01-03", 0.30),                              # mean 0.30
    ]
    out = _per_spec_realized_series(_frame(rows), "champion", 2)
    out = out.sort_values("prediction_date").reset_index(drop=True)
    assert list(out["model_version"]) == ["V1", "V1", "V1"]
    assert list(out["stage"]) == ["champion"] * 3
    assert [round(v, 4) for v in out["realized_alpha"]] == [0.10, 0.20, 0.30]
    assert [round(v, 4) for v in out["rolling_realized_alpha"]] == [0.10, 0.15, 0.25]
    assert list(out["n_predictions"]) == [2, 1, 1]


def test_null_model_version_labelled_legacy():
    rows = [(None, "2026-01-01", 0.05)]
    out = _per_spec_realized_series(_frame(rows), "champion", 8)
    assert out.iloc[0]["model_version"] == "champion-legacy"


def test_series_unions_live_and_shadow_sorted(monkeypatch):
    live = _frame([("V_live", "2026-01-01", 0.10)])
    shadow = _frame([("V2", "2026-01-01", 0.20)])

    def _fake_query(sql, params=None):
        return shadow if "predictor_outcomes_shadow" in sql else live

    monkeypatch.setattr(db_loader, "query_research_db", _fake_query)
    out = get_per_spec_realized_alpha_series()
    assert set(out["model_version"]) == {"V_live", "V2"}
    assert set(out["stage"]) == {"champion", "challenger"}
    # Sorted by model_version then prediction_date.
    assert list(out["model_version"]) == sorted(out["model_version"])


def test_series_degrades_to_champion_only_when_shadow_missing(monkeypatch):
    live = _frame([("V_live", "2026-01-01", 0.10)])

    def _fake_query(sql, params=None):
        return pd.DataFrame() if "predictor_outcomes_shadow" in sql else live

    monkeypatch.setattr(db_loader, "query_research_db", _fake_query)
    out = get_per_spec_realized_alpha_series()
    assert len(out) == 1 and out.iloc[0]["stage"] == "champion"


def test_series_empty_when_no_data(monkeypatch):
    monkeypatch.setattr(db_loader, "query_research_db", lambda sql, params=None: pd.DataFrame())
    assert get_per_spec_realized_alpha_series().empty
