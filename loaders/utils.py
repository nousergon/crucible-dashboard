"""
Utility helpers for the Alpha Engine Dashboard loaders and pages.
"""

import numpy as np
import pandas as pd


def risk_contribution_shares(
    target_weights: list[float], covariance_daily: list[list[float]] | None
) -> np.ndarray:
    """Per-asset share of total portfolio variance, in percent.

    For weights ``w`` and covariance ``Σ``: the risk contribution of asset i is
    ``rc_i = w_i · (Σw)_i`` and the portfolio variance is ``wᵀΣw = Σ rc_i``.
    Returns ``rc_i / wᵀΣw · 100`` (the contributions sum to 100 for a fully
    invested book). Used by the Optimizer Decision page on the optimizer shadow
    log's persisted daily covariance — no re-solve.

    Returns an all-NaN array (length = len(target_weights)) when the covariance
    is absent / not square / dimension-mismatched, or portfolio variance ≤ 0.
    """
    n = len(target_weights)
    nan = np.full(n, np.nan)
    if not isinstance(covariance_daily, list) or len(covariance_daily) != n:
        return nan
    if not all(isinstance(row, list) and len(row) == n for row in covariance_daily):
        return nan
    w = np.array(
        [float(x) if isinstance(x, (int, float)) else 0.0 for x in target_weights]
    )
    sigma = np.array(covariance_daily, dtype=float)
    sigma_w = sigma @ w
    port_var = float(w @ sigma_w)
    if port_var <= 0:
        return nan
    return w * sigma_w / port_var * 100.0


def safe_column(df: pd.DataFrame, *candidates: str) -> str | None:
    """Return the first column name from *candidates* that exists in *df*, or None."""
    for col in candidates:
        if col in df.columns:
            return col
    return None


def production_feature_set(feature_list: dict | None) -> set[str]:
    """Flatten the L2 + L1 feature lists from `feature_list.json` into a single set.
    Tolerates None / missing keys / non-list values — returns empty set in those cases.
    """
    if not isinstance(feature_list, dict):
        return set()
    prod: set[str] = set(feature_list.get("l2_features") or [])
    for l1_feats in (feature_list.get("l1_features") or {}).values():
        if isinstance(l1_feats, list):
            prod.update(l1_feats)
    return prod


def research_feature_set(*dfs: pd.DataFrame | None) -> set[str]:
    """Union of columns across feature-store parquets, excluding `ticker` and
    `date` index columns. Tolerates None DataFrames — skips them.
    """
    research: set[str] = set()
    for df in dfs:
        if df is None:
            continue
        research.update(c for c in df.columns if c not in ("ticker", "date"))
    return research
