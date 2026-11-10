"""Unit tests for sector allocation and rotation figures."""

import sys
from pathlib import Path
from typing import cast

import pandas as pd
import plotly.graph_objects as go
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from charts.portfolio_chart import (  # noqa: E402
    make_sector_allocation_chart,
    make_sector_rotation_chart,
)


@pytest.mark.parametrize(
    ("frame", "title"),
    [
        (None, "No data"),
        (pd.DataFrame(), "No data"),
        (pd.DataFrame({"market_value": [1]}), "No data"),
        (pd.DataFrame({"sector": ["Tech"]}), "No market_value column"),
        (pd.DataFrame({"sector": ["Tech", "Energy"], "market_value": [1, -1]}),
         "Zero total value"),
    ],
)
def test_allocation_placeholder(frame: pd.DataFrame | None, title: str) -> None:
    fig = make_sector_allocation_chart(cast(pd.DataFrame, frame))

    assert len(fig.data) == 0
    assert fig.layout.title.text == f"Sector Allocation — {title}"


def test_allocation_aggregates_values_and_colors_weight_boundaries() -> None:
    frame = pd.DataFrame(
        {"sector": ["Tech", "Tech", "Energy", "Health", "Other", "Tech"],
         "market_value": [10, "25", 25, 20, 20, "invalid"]},
        index=[9, 7, 5, 3, 1, 0],
    )
    original = frame.copy(deep=True)

    fig = make_sector_allocation_chart(frame)

    pd.testing.assert_frame_equal(frame, original)
    assert len(fig.data) == 1
    pie = fig.data[0]
    assert isinstance(pie, go.Pie)
    assert pie.hole == 0.45
    assert dict(zip(pie.labels, pie.values)) == {
        "Tech": 35, "Energy": 25, "Health": 20, "Other": 20,
    }
    assert dict(zip(pie.labels, pie.marker.colors)) == {
        "Tech": "#dc3545", "Energy": "#ffc107",
        "Health": "#28a745", "Other": "#28a745",
    }


def test_rotation_no_data() -> None:
    fig = make_sector_rotation_chart([])

    assert len(fig.data) == 0
    assert fig.layout.title.text == "Sector Rotation — No data"


def test_rotation_stacks_sector_percentages_and_drops_zero_total_dates() -> None:
    records: list[dict[str, str | int]] = [
        {"date": "2026-01-03", "sector": "Tech", "market_value": 10},
        {"date": "2026-01-01", "sector": "Tech", "market_value": 10},
        {"date": "2026-01-01", "sector": "Tech", "market_value": 20},
        {"date": "2026-01-01", "sector": "Energy", "market_value": 10},
        {"date": "2026-01-02", "sector": "Tech", "market_value": 0},
        {"date": "2026-01-02", "sector": "Energy", "market_value": 0},
    ]

    fig = make_sector_rotation_chart(records)

    assert len(fig.data) == 2
    expected = {"Tech": [75.0, 100.0], "Energy": [25.0, 0.0]}
    totals = [0.0, 0.0]
    for trace in fig.data:
        assert isinstance(trace, go.Scatter)
        assert trace.stackgroup == "one"
        assert trace.mode == "lines"
        assert list(trace.x) == list(pd.to_datetime(["2026-01-01", "2026-01-03"]))
        assert list(trace.y) == pytest.approx(expected[trace.name])
        totals = [total + value for total, value in zip(totals, trace.y)]
    assert {trace.name for trace in fig.data} == set(expected)
    assert totals == pytest.approx([100.0, 100.0])
    assert any(shape.y0 == 25 and shape.y1 == 25 for shape in fig.layout.shapes)


@pytest.mark.parametrize(
    ("time_range", "start"),
    [("30d", 3), ("90d", 1), ("all", 0), ("unknown", 0)],
)
def test_rotation_time_ranges_include_cutoff(time_range: str, start: int) -> None:
    latest = pd.Timestamp("2026-06-30")
    dates = [latest - pd.Timedelta(days=days) for days in [91, 90, 31, 30, 0]]
    records: list[dict[str, str | int]] = [
        {"date": date.strftime("%Y-%m-%d"), "sector": "Tech", "market_value": 1}
        for date in reversed(dates)
    ]

    fig = make_sector_rotation_chart(records, time_range=time_range)

    assert len(fig.data) == 1
    trace = fig.data[0]
    assert isinstance(trace, go.Scatter)
    assert list(trace.x) == dates[start:]
    assert list(trace.y) == pytest.approx([100.0] * len(dates[start:]))
