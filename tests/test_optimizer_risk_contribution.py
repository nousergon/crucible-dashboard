"""Tests for loaders.utils.risk_contribution_shares — the per-asset variance
share computed on the optimizer shadow log's persisted daily covariance for the
Optimizer Decision page.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from loaders.utils import risk_contribution_shares


def test_shares_sum_to_100():
    w = [0.5, 0.3, 0.2]
    cov = [[0.04, 0.01, 0.00],
           [0.01, 0.09, 0.02],
           [0.00, 0.02, 0.16]]
    out = risk_contribution_shares(w, cov)
    assert np.isfinite(out).all()
    assert out.sum() == pytest.approx(100.0)


def test_diagonal_equal_weight_equal_shares():
    # Equal weights + identical variances + zero covariance → equal shares.
    w = [0.5, 0.5]
    cov = [[0.04, 0.0], [0.0, 0.04]]
    out = risk_contribution_shares(w, cov)
    assert out == pytest.approx([50.0, 50.0])


def test_diagonal_proportional_to_w2_var():
    # Diagonal Σ → rc_i ∝ w_i² · var_i.
    w = [0.7, 0.3]
    cov = [[0.04, 0.0], [0.0, 0.09]]
    out = risk_contribution_shares(w, cov)
    rc = np.array([0.7**2 * 0.04, 0.3**2 * 0.09])
    expected = rc / rc.sum() * 100.0
    assert out == pytest.approx(expected)


def test_none_covariance_is_all_nan():
    out = risk_contribution_shares([0.5, 0.5], None)
    assert len(out) == 2 and np.isnan(out).all()


def test_dimension_mismatch_is_all_nan():
    out = risk_contribution_shares([0.5, 0.5, 0.0], [[0.04, 0.0], [0.0, 0.04]])
    assert len(out) == 3 and np.isnan(out).all()


def test_non_square_row_is_all_nan():
    out = risk_contribution_shares([0.5, 0.5], [[0.04, 0.0], [0.0]])
    assert np.isnan(out).all()


def test_zero_variance_is_all_nan():
    # All-zero weights (e.g. cash-only book) → portfolio variance 0 → NaN.
    out = risk_contribution_shares([0.0, 0.0], [[0.04, 0.0], [0.0, 0.09]])
    assert np.isnan(out).all()


def test_cash_row_zero_covariance_contributes_nothing():
    # CASH has an all-zero covariance row/col → 0% risk contribution; the
    # equity names absorb 100%.
    w = [0.6, 0.4, 0.0]  # equity, equity, cash
    cov = [[0.04, 0.01, 0.0],
           [0.01, 0.09, 0.0],
           [0.0, 0.0, 0.0]]
    out = risk_contribution_shares(w, cov)
    assert out[2] == pytest.approx(0.0)
    assert out[:2].sum() == pytest.approx(100.0)
