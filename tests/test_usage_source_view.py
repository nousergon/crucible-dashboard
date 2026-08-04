"""Tests for the per-source cost-breakdown helpers in shared/usage_source_view.py.

Pure-function contracts over a hand-built df_model — no S3, no streamlit —
mirroring tests/test_expenses_page.py's source-assertion style.
"""
from __future__ import annotations

import pandas as pd

from shared.usage_source_view import (
    AUTONOMOUS_SOURCES,
    cache_read_pct,
    daily_cost_by_source,
    source_breakdown,
)


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_cache_read_pct_none_when_no_tokens():
    """A zero-total group returns None, not 0% — 'not reported' must never
    render as a healthy 0% (mirrors the Backlog Groom page convention)."""
    assert cache_read_pct(_df([{"total": 0, "cache_read_input_tokens": 0}])) is None


def test_cache_read_pct_ratio():
    df = _df([
        {"total": 100, "cache_read_input_tokens": 96},
        {"total": 100, "cache_read_input_tokens": 4},
    ])
    assert cache_read_pct(df) == 50.0


def test_source_breakdown_empty_df():
    """An empty df_model yields an empty frame with the expected schema —
    the caller renders the 'no data' caption, never a KeyError."""
    out = source_breakdown(pd.DataFrame())
    assert out.empty
    assert "cost_usd" in out.columns
    assert "cache_read_pct" in out.columns


def test_source_breakdown_one_row_per_source_sorted_by_cost():
    """Groom (expensive) sorts above interactive (cheap); each row carries
    cost, cache-read %, WET, and the autonomous flag."""
    df = _df([
        {"date": "2026-08-01", "source": "groom", "model": "deepseek-pro",
         "cost_usd": 5.86, "total": 61250364, "cache_read_input_tokens": 58841856,
         "wet": 1698412, "input_tokens": 1941213},
        {"date": "2026-08-02", "source": "groom", "model": "deepseek-flash",
         "cost_usd": 0.03, "total": 1376594, "cache_read_input_tokens": 1225216,
         "wet": 13584, "input_tokens": 98570},
        {"date": "2026-08-02", "source": "interactive", "model": "deepseek-flash",
         "cost_usd": 0.15, "total": 500000, "cache_read_input_tokens": 400000,
         "wet": 80000, "input_tokens": 100000},
    ])
    out = source_breakdown(df)
    assert list(out["source"]) == ["groom", "interactive"]
    assert abs(out.loc[out["source"] == "groom", "cost_usd"].iloc[0] - 5.89) < 1e-9  # 5.86 + 0.03
    # groom spans 2 distinct dates → run_days == 2
    assert out.loc[out["source"] == "groom", "run_days"].iloc[0] == 2
    assert bool(out.loc[out["source"] == "groom", "is_autonomous"].iloc[0]) is True
    assert bool(out.loc[out["source"] == "interactive", "is_autonomous"].iloc[0]) is False
    # cache_read_pct on the merged groom rows: (58841856 + 1225216) / (61250364 + 1376594)
    groom_cr = out.loc[out["source"] == "groom", "cache_read_pct"].iloc[0]
    assert groom_cr is not None and 95.0 < groom_cr < 99.0


def test_daily_cost_by_source_wide_pivot():
    """One column per source, indexed by date, 0.0-filled gaps, date-sorted."""
    df = _df([
        {"date": "2026-08-02", "source": "groom", "cost_usd": 0.03},
        {"date": "2026-08-01", "source": "groom", "cost_usd": 5.86},
        {"date": "2026-08-01", "source": "interactive", "cost_usd": 0.15},
    ])
    daily = daily_cost_by_source(df)
    assert list(daily.index) == ["2026-08-01", "2026-08-02"]
    assert set(daily.columns) == {"groom", "interactive"}
    assert daily.loc["2026-08-01", "groom"] == 5.86
    assert daily.loc["2026-08-02", "interactive"] == 0.0  # gap filled


def test_daily_cost_by_source_filters_to_named_sources():
    """When sources=['groom'], interactive is excluded entirely."""
    df = _df([
        {"date": "2026-08-01", "source": "groom", "cost_usd": 5.86},
        {"date": "2026-08-01", "source": "interactive", "cost_usd": 0.15},
    ])
    daily = daily_cost_by_source(df, sources=["groom"])
    assert list(daily.columns) == ["groom"]
    assert "interactive" not in daily.columns


def test_autonomous_sources_is_a_set_containing_groom():
    """The groom source is the canonical autonomous member; a frozenset so a
    new source is additive without touching call sites."""
    assert "groom" in AUTONOMOUS_SOURCES
    assert isinstance(AUTONOMOUS_SOURCES, frozenset)
