"""Unit tests for the daily and cumulative alpha figure."""

import sys
from pathlib import Path
from typing import cast

import pandas as pd
import plotly.graph_objects as go
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from charts.alpha_chart import make_alpha_chart  # noqa: E402


@pytest.mark.parametrize("frame", [None, pd.DataFrame()])
def test_alpha_no_data(frame: pd.DataFrame | None) -> None:
    # The builder accepts None at runtime despite its narrower annotation.
    fig = make_alpha_chart(cast(pd.DataFrame, frame))

    assert len(fig.data) == 0
    assert fig.layout.title.text == "Daily Alpha — No data available"


@pytest.mark.parametrize(
    "values", [[-0.02, 0.01, 0.0], [-2.0, 1.0, 0.0]],
    ids=["decimal", "percent"],
)
def test_alpha_sorted_percent_values_and_cumulative_sum(values: list[float]) -> None:
    frame = pd.DataFrame(
        {"date": ["2026-01-03", "2026-01-01", "2026-01-02"],
         "daily_alpha_pct": values},
        index=[8, 2, 5],
    )
    original = frame.copy(deep=True)

    fig = make_alpha_chart(frame)

    pd.testing.assert_frame_equal(frame, original)
    assert len(fig.data) == 2
    bar, cumulative = fig.data
    assert isinstance(bar, go.Bar)
    assert isinstance(cumulative, go.Scatter)
    expected_dates = list(pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]))
    assert list(bar.x) == expected_dates
    assert list(cumulative.x) == expected_dates
    assert list(bar.y) == pytest.approx([1.0, 0.0, -2.0])
    assert list(bar.marker.color) == ["#2ca02c", "#2ca02c", "#d62728"]
    assert list(cumulative.y) == pytest.approx([1.0, 1.0, -1.0])
    # Plotly canonicalizes the primary-axis alias y1 to y.
    assert bar.yaxis == "y"
    assert cumulative.yaxis == "y2"
