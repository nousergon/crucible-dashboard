"""Tests for `charts.attribution_chart.make_attribution_chart` (config#1481).

Guards the producer/consumer schema fix: the chart previously read a stale
FLAT ``{technical,news,research}_{10d,30d}`` shape via ``.get(..., 0.0)``,
which never matched the real backtester producer output
(``analysis/attribution.py::compute_attribution`` in crucible-backtester),
so every bar silently defaulted to 0.0 (config#1456 / crucible-dashboard#280
left this unmigrated pending the crucible-backtester#428 21d rename).

The real producer schema (confirmed from crucible-backtester#428, merged
2026-07-01) is NESTED and keyed by the canonical 21d targets:

    {
        "status": "ok",
        "correlations": {
            "quant": {"beat_spy_21d": 0.12, "return_21d": 0.09, ...},
            "qual": {"beat_spy_21d": ..., "return_21d": ..., ...},
        },
        "ranking_21d": ["qual", "quant"],
        ...
    }

These tests use a fixture built from that real nested schema and assert the
chart renders non-zero attribution values from it, plus that genuinely
missing keys still default gracefully (the defaulting mechanism itself was
never the bug — reading the wrong keys was).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from charts.attribution_chart import make_attribution_chart  # noqa: E402


def _real_producer_fixture() -> dict:
    """A realistic ``attribution.json`` payload matching the real nested
    post-#428 producer schema (quant/qual, 21d-suffixed keys)."""
    return {
        "status": "ok",
        "rows_analyzed": 512,
        "correlations": {
            "quant": {
                "beat_spy_21d": 0.184,
                "return_21d": 0.121,
                "beat_spy_21d_fdr_significant": True,
                "return_21d_fdr_significant": True,
            },
            "qual": {
                "beat_spy_21d": -0.057,
                "return_21d": -0.033,
                "beat_spy_21d_fdr_significant": False,
                "return_21d_fdr_significant": False,
            },
        },
        "ranking_21d": ["quant", "qual"],
        "note": "Primary attribution is multivariate ...",
    }


def _trace_by_name(fig, name):
    for t in fig.data:
        if t.name == name:
            return t
    raise AssertionError(f"no trace named {name!r} among {[t.name for t in fig.data]}")


def test_reads_nested_real_producer_schema_nonzero_values():
    """The chart must read the real nested {quant,qual}/{beat_spy,return}_21d
    schema and render the actual non-zero correlations, not silently default
    every value to 0.0."""
    fig = make_attribution_chart(_real_producer_fixture())

    beat_spy_trace = _trace_by_name(fig, "beat_spy_21d Correlation")
    return_trace = _trace_by_name(fig, "return_21d Correlation")

    # y order is [quant, qual] per the chart's fixed sub_scores list.
    assert list(beat_spy_trace.x) == pytest.approx([0.184, -0.057])
    assert list(return_trace.x) == pytest.approx([0.121, -0.033])
    assert list(beat_spy_trace.y) == ["Quant", "Qual"]

    # Not all zero — this is the regression this fix guards against.
    assert any(v != 0.0 for v in beat_spy_trace.x)
    assert any(v != 0.0 for v in return_trace.x)


def test_missing_sub_score_defaults_to_zero_gracefully():
    """A sub-score genuinely absent from correlations (e.g. insufficient
    valid rows for that pair) should default to 0.0 rather than raise —
    the defaulting mechanism itself is correct; only the keys read were
    wrong."""
    data = {
        "status": "ok",
        "correlations": {
            "quant": {"beat_spy_21d": 0.2, "return_21d": 0.1},
            # "qual" entirely absent.
        },
        "ranking_21d": ["quant"],
    }
    fig = make_attribution_chart(data)

    beat_spy_trace = _trace_by_name(fig, "beat_spy_21d Correlation")
    assert list(beat_spy_trace.x) == pytest.approx([0.2, 0.0])


def test_missing_target_key_within_sub_score_defaults_to_zero():
    """A sub-score present but missing one of the two target keys (e.g. a
    correlation that couldn't be computed, stored as None) should default
    that single value to 0.0."""
    data = {
        "status": "ok",
        "correlations": {
            "quant": {"beat_spy_21d": 0.2, "return_21d": None},
            "qual": {"beat_spy_21d": None, "return_21d": -0.1},
        },
    }
    fig = make_attribution_chart(data)

    beat_spy_trace = _trace_by_name(fig, "beat_spy_21d Correlation")
    return_trace = _trace_by_name(fig, "return_21d Correlation")

    assert list(beat_spy_trace.x) == pytest.approx([0.2, 0.0])
    assert list(return_trace.x) == pytest.approx([0.0, -0.1])


def test_empty_data_renders_placeholder():
    fig = make_attribution_chart({})
    assert fig.layout.title.text == "Sub-Score Attribution — No data available"
    assert len(fig.data) == 0


def test_insufficient_data_status_renders_placeholder():
    """``compute_attribution`` returns status != "ok" (e.g.
    "insufficient_data") with no ``correlations`` key at all when there
    aren't enough rows yet — the chart must not crash on that shape."""
    fig = make_attribution_chart(
        {"status": "insufficient_data", "rows_populated": 12, "note": "..."}
    )
    assert fig.layout.title.text == "Sub-Score Attribution — No data available"
    assert len(fig.data) == 0
