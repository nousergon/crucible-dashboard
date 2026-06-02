"""Tests for the champion/challenger per-version scorecard (L4469 Phase 3).

Covers _per_version_metrics (per-date Spearman rank-IC + hit-rate, NaN-safe) and
get_model_version_scorecard (union live champion + shadow challengers, sorted).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock streamlit + s3_loader before importing db_loader (mirrors test_db_loader).
sys.modules.setdefault("streamlit", MagicMock())
sys.modules.setdefault("yaml", __import__("yaml") if "yaml" in sys.modules else MagicMock())
_mock_s3 = MagicMock()
_mock_s3.load_config.return_value = {
    "s3": {"research_bucket": "test-bucket"}, "paths": {"research_db": "research.db"},
}
sys.modules["loaders.s3_loader"] = _mock_s3

from loaders import db_loader  # noqa: E402
from loaders.db_loader import _per_version_metrics, get_model_version_scorecard  # noqa: E402


def _frame(rows):
    return pd.DataFrame(rows, columns=[
        "model_version", "prediction_date", "p_up", "actual_log_alpha", "correct",
    ])


def test_per_version_metrics_empty():
    assert _per_version_metrics(pd.DataFrame(), "champion").empty


def test_per_version_metrics_rank_ic_and_hitrate():
    # One version, two dates; within each date p_up rank tracks realized alpha
    # perfectly → per-date Spearman = +1 → rank_ic = 1.0. hit_rate = 3/4.
    rows = [
        ("V1", "2026-01-01", 0.6, 0.05, 1),
        ("V1", "2026-01-01", 0.4, -0.02, 1),
        ("V1", "2026-01-02", 0.7, 0.03, 1),
        ("V1", "2026-01-02", 0.3, 0.01, 0),
    ]
    out = _per_version_metrics(_frame(rows), "champion")
    r = out.iloc[0]
    assert r["model_version"] == "V1" and r["stage"] == "champion"
    assert abs(r["rank_ic"] - 1.0) < 1e-9
    assert abs(r["hit_rate"] - 0.75) < 1e-9
    assert r["n_predictions"] == 4 and r["n_dates"] == 2


def test_null_model_version_labelled_legacy():
    rows = [
        (None, "2026-01-01", 0.6, 0.05, 1),
        (None, "2026-01-01", 0.4, -0.02, 1),
    ]
    out = _per_version_metrics(_frame(rows), "champion")
    assert out.iloc[0]["model_version"] == "champion-legacy"


def test_scorecard_unions_live_and_shadow_sorted_by_ic(monkeypatch):
    # champion V_live: anti-correlated within the date → rank_ic = -1.
    live = _frame([
        ("V_live", "2026-01-01", 0.6, -0.05, 0),
        ("V_live", "2026-01-01", 0.4, 0.02, 0),
    ])
    # challenger V2: perfectly correlated → rank_ic = +1.
    shadow = _frame([
        ("V2", "2026-01-01", 0.6, 0.05, 1),
        ("V2", "2026-01-01", 0.4, -0.02, 1),
    ])

    def _fake_query(sql, params=None):
        return shadow if "predictor_outcomes_shadow" in sql else live

    monkeypatch.setattr(db_loader, "query_research_db", _fake_query)
    out = get_model_version_scorecard()
    assert list(out["model_version"]) == ["V2", "V_live"]  # higher IC first
    assert list(out["stage"]) == ["challenger", "champion"]


def test_scorecard_degrades_to_champion_only_when_shadow_missing(monkeypatch):
    live = _frame([
        ("V_live", "2026-01-01", 0.6, 0.05, 1),
        ("V_live", "2026-01-01", 0.4, -0.02, 1),
    ])

    def _fake_query(sql, params=None):
        # Missing shadow table → query_research_db returns empty (its contract).
        return pd.DataFrame() if "predictor_outcomes_shadow" in sql else live

    monkeypatch.setattr(db_loader, "query_research_db", _fake_query)
    out = get_model_version_scorecard()
    assert len(out) == 1 and out.iloc[0]["stage"] == "champion"


def test_scorecard_empty_when_no_data(monkeypatch):
    monkeypatch.setattr(db_loader, "query_research_db", lambda sql, params=None: pd.DataFrame())
    assert get_model_version_scorecard().empty
