"""Tests for `charts.predictor_chart.make_model_drift_chart`.

Guards the 6-month (180-row) long-term degradation trend added for config#955:
the line must appear once enough resolved predictions exist and must stay absent
when the window is too sparse, without disturbing the 30d/90d traces.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from charts.predictor_chart import make_model_drift_chart


def _outcomes(n: int) -> pd.DataFrame:
    """n resolved daily predictions, alternating hit/miss (deterministic)."""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "prediction_date": dates,
            "correct": [i % 2 for i in range(n)],
        }
    )


def _line_names(fig):
    return {t.name for t in fig.data if t.name}


def test_six_month_line_present_with_enough_history():
    fig = make_model_drift_chart(_outcomes(200))
    names = _line_names(fig)
    assert "30-day rolling" in names
    assert "90-day rolling" in names
    assert "6-month rolling" in names


def test_no_rolling_traces_below_chart_floor():
    # Below the ≥60-resolved chart floor → placeholder figure, no rolling traces.
    fig = make_model_drift_chart(_outcomes(40))
    assert _line_names(fig) == set()


def test_six_month_present_exactly_at_sixty():
    fig = make_model_drift_chart(_outcomes(60))
    assert "6-month rolling" in _line_names(fig)
