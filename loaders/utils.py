"""
Utility helpers for the Alpha Engine Dashboard loaders and pages.
"""

import pandas as pd


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
