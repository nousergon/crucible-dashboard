"""Tests for ``shared.cache_hit_rate`` — gate G6 of the prompt-caching policy.

The subtle part is the DENOMINATOR, not the division. Two mechanisms report
differently and the wrong denominator produces a confidently wrong number
rather than an error, which is the same class of silent failure the policy
exists to prevent. These tests pin that rule.
"""
from __future__ import annotations

import pandas as pd
import pytest

from shared import cache_hit_rate as chr


def _row(**kw):
    base = {
        "model": "deepseek-v4-flash",
        "cache_read_tokens": 0,
        "input_tokens": 0,
        "cost_usd": 0.0,
    }
    base.update(kw)
    return base


class TestBasis:
    def test_reported_miss_wins_over_model_name(self):
        """A provider-reported miss is a direct statement; the prefix match is
        only an inference. If both are available the report wins."""
        assert chr.cache_basis("claude-opus-5", 100) == chr.BASIS_REPORTED_MISS

    def test_anthropic_model_without_miss_uses_uncached_input(self):
        assert chr.cache_basis("claude-haiku-4-5", None) == chr.BASIS_UNCACHED_INPUT
        assert chr.cache_basis("claude-sonnet-5", pd.NA) == chr.BASIS_UNCACHED_INPUT

    def test_non_anthropic_without_miss_is_unknown(self):
        assert chr.cache_basis("deepseek-v4-pro", None) == chr.BASIS_UNKNOWN
        assert chr.cache_basis("kimi-k3", pd.NA) == chr.BASIS_UNKNOWN

    def test_missing_model_is_unknown_not_a_crash(self):
        assert chr.cache_basis(None, None) == chr.BASIS_UNKNOWN
        assert chr.cache_basis(float("nan"), None) == chr.BASIS_UNKNOWN


class TestDenominator:
    def test_reported_miss_denominator_is_hit_plus_miss(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            _row(cache_read_tokens=900, prompt_cache_miss_tokens=100,
                 input_tokens=1000),
        ]))
        # input_tokens is the TOTAL on this mechanism — using it would give
        # 900/1900, not 900/1000.
        assert f.loc[0, "cache_denominator"] == 1000
        assert chr.hit_rate(f) == pytest.approx(0.9)

    def test_anthropic_denominator_is_hit_plus_uncached_input(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            _row(model="claude-opus-5", cache_read_tokens=800, input_tokens=200),
        ]))
        assert f.loc[0, "cache_denominator"] == 1000
        assert chr.hit_rate(f) == pytest.approx(0.8)

    def test_absent_miss_column_does_not_fabricate_a_perfect_rate(self):
        """The regression this module exists to prevent.

        A pre-krepis-0.19.2 partition has no ``prompt_cache_miss_tokens``
        column at all. Zero-filling it would collapse the denominator onto the
        cache-read count and render 100% for every historical row.
        """
        legacy = pd.DataFrame([
            {"model": "deepseek-v4-flash", "cache_read_tokens": 900,
             "input_tokens": 1000, "cost_usd": 0.01},
        ])
        assert "prompt_cache_miss_tokens" not in legacy.columns
        f = chr.with_cache_denominator(legacy)
        assert f.loc[0, "cache_basis"] == chr.BASIS_UNKNOWN
        assert chr.hit_rate(f) is None, "must be unknown, never 100%"

    def test_input_is_not_mutated(self):
        original = pd.DataFrame([_row(cache_read_tokens=5)])
        before = original.copy(deep=True)
        chr.with_cache_denominator(original)
        pd.testing.assert_frame_equal(original, before)


class TestHitRate:
    def test_no_telemetry_returns_none_not_zero(self):
        """0% would read as total cache failure; the truth is 'not reported'."""
        f = chr.with_cache_denominator(pd.DataFrame([_row(model="kimi-k3")]))
        assert chr.hit_rate(f) is None

    def test_unknown_rows_are_excluded_not_counted_as_misses(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            _row(cache_read_tokens=900, prompt_cache_miss_tokens=100),
            _row(model="kimi-k3", cache_read_tokens=0, input_tokens=5000),
        ]))
        # Were the unknown row folded in as 5000 misses, the rate would
        # collapse to 900/6000 = 15%.
        assert chr.hit_rate(f) == pytest.approx(0.9)
        assert len(chr.measurable(f)) == 1

    def test_zero_denominator_row_is_excluded(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            _row(cache_read_tokens=0, prompt_cache_miss_tokens=0),
        ]))
        assert chr.hit_rate(f) is None

    def test_mixed_mechanisms_aggregate_on_their_own_denominators(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            _row(cache_read_tokens=900, prompt_cache_miss_tokens=100),
            _row(model="claude-opus-5", cache_read_tokens=500, input_tokens=500),
        ]))
        assert chr.hit_rate(f) == pytest.approx(1400 / 2000)


class TestByModel:
    def test_groups_and_orders_by_denominator(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            _row(model="small", cache_read_tokens=9, prompt_cache_miss_tokens=1),
            _row(model="big", cache_read_tokens=500, prompt_cache_miss_tokens=500),
            _row(model="big", cache_read_tokens=500, prompt_cache_miss_tokens=500),
        ]))
        out = chr.by_model(f)
        assert list(out["model"]) == ["big", "small"]
        assert out.iloc[0]["hit_rate"] == pytest.approx(0.5)
        assert out.iloc[0]["calls"] == 2
        assert out.iloc[1]["hit_rate"] == pytest.approx(0.9)

    def test_empty_input_returns_empty_frame_with_schema(self):
        f = chr.with_cache_denominator(pd.DataFrame([_row(model="kimi-k3")]))
        out = chr.by_model(f)
        assert out.empty
        assert "hit_rate" in out.columns

    def test_missing_cost_column_is_tolerated(self):
        f = chr.with_cache_denominator(pd.DataFrame([
            {"model": "deepseek-v4-flash", "cache_read_tokens": 9,
             "input_tokens": 0, "prompt_cache_miss_tokens": 1},
        ]))
        out = chr.by_model(f)
        assert out.iloc[0]["hit_rate"] == pytest.approx(0.9)
