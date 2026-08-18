"""Drift guard: this repo's Sharpe path vs nousergon_lib (config-I7597).

`shared/accuracy_metrics.py::compute_sharpe` used to be an independent
re-implementation of `mean / std * sqrt(252)`. It now calls
`nousergon_lib.quant.riskstats.sharpe_ratio`, so it cannot drift — this file
pins that, plus the two conventions the library deliberately does not decide:
this surface's `min_rows` display floor, and undefined-is-None.

CORPUS is kept byte-identical to
`nousergon-lib/tests/test_quant_riskstats_drift_corpus.py`, which pins the
library's own answers against values written out from the definition.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from nousergon_lib.quant import riskstats

from shared.accuracy_metrics import compute_sharpe

# Keep byte-identical to the nousergon-lib copy.
CORPUS: dict[str, list[float]] = {
    "mixed": [0.01, -0.02, 0.015, -0.005, 0.03, -0.01, 0.0, 0.02, -0.03, 0.005],
    "all_positive": [0.01, 0.02, 0.005, 0.03, 0.015],
    "all_negative": [-0.01, -0.02, -0.005, -0.04],
    "all_zero": [0.0, 0.0, 0.0, 0.0, 0.0],
    "zero_vol_positive": [0.01] * 8,
    "zero_vol_negative": [-0.01] * 8,
    "two_obs": [0.01, -0.01],
    "single_obs": [0.02],
    "empty": [],
    "tiny_downside": [0.01, 0.02, 0.03, -1e-9],
}


def _ref_sharpe(r: list[float]) -> float | None:
    """Sharpe written out from the definition — no lib call."""
    if len(r) < 2:
        return None
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
    if sd == 0:
        return None
    return (mean / sd) * math.sqrt(252)


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_compute_sharpe_matches_the_definition(name: str) -> None:
    """min_rows lowered to 2 so the statistic, not the display floor, is tested."""
    r = CORPUS[name]
    got = compute_sharpe(pd.Series(r, dtype="float64"), min_rows=2)
    want = _ref_sharpe(r)
    if want is None:
        assert got is None, f"{name}: expected undefined, got {got}"
    else:
        assert got == pytest.approx(want, rel=1e-9, abs=1e-12), name


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_compute_sharpe_matches_the_library(name: str) -> None:
    r = CORPUS[name]
    got = compute_sharpe(pd.Series(r, dtype="float64"), min_rows=2)
    want = riskstats.sharpe_ratio(r)
    if want is None:
        assert got is None, name
    else:
        assert got == pytest.approx(want, rel=1e-9, abs=1e-12), name


def test_min_rows_is_a_display_floor_not_part_of_the_statistic() -> None:
    r = CORPUS["mixed"]  # n = 10
    assert compute_sharpe(pd.Series(r), min_rows=30) is None
    assert compute_sharpe(pd.Series(r), min_rows=10) is not None


def test_undefined_is_none_never_a_rendered_number() -> None:
    """A flat series has no Sharpe; it must not render as inf, nan or 0."""
    got = compute_sharpe(pd.Series(CORPUS["zero_vol_positive"]), min_rows=2)
    assert got is None
    got = compute_sharpe(pd.Series(CORPUS["single_obs"]), min_rows=1)
    assert got is None


def test_nans_are_dropped_before_the_statistic() -> None:
    r = CORPUS["mixed"]
    with_nans = pd.Series(r + [float("nan"), float("nan")], dtype="float64")
    assert compute_sharpe(with_nans, min_rows=2) == pytest.approx(
        compute_sharpe(pd.Series(r, dtype="float64"), min_rows=2), rel=1e-12
    )
