"""Pairwise return-correlation of current holdings (config#953).

Pure pandas data-shaping over the per-(date, ticker) daily-return long frame
produced by :func:`shared.attribution.build_long_frame` (itself built from the
executor's ``eod_pnl`` ``positions_snapshot`` history). No Streamlit / S3 I/O
here, so it is fully unit-testable — the Portfolio view does the rendering.

The concentration half of config#953 (sector HHI) already ships in
``views/1_Portfolio.py``; this module supplies the correlation half that was
previously stubbed as "requires price history integration". The price history
*is* reachable: ``build_long_frame`` exposes a per-position ``daily_return_pct``
series per holding (stored post-2026-04-20, reconstructed from ``closing_price``
before that), which is exactly what a Pearson correlation needs.

Pearson correlation is scale-invariant, so it does not matter that
``daily_return_pct`` is in percent rather than decimal.
"""
from __future__ import annotations

import pandas as pd

from shared.attribution import COL_RETURN

__all__ = [
    "holdings_return_matrix",
    "correlation_matrix",
    "high_correlation_pairs",
]


def holdings_return_matrix(
    long_df: pd.DataFrame | None,
    tickers: list[str] | None = None,
) -> pd.DataFrame:
    """Pivot the attribution long frame into a (date × ticker) daily-return matrix.

    Rows = trading dates, columns = tickers, cells = ``daily_return_pct``; NaN
    where a ticker was not held on a given date. When ``tickers`` is provided the
    columns are restricted to (and ordered by) that set — typically the *current*
    holdings, so we correlate the names the operator actually holds rather than
    every name ever held. Tickers absent from the history are dropped.

    Returns an empty frame when there is no usable return history.
    """
    if (
        long_df is None
        or long_df.empty
        or COL_RETURN not in long_df.columns
        or "date" not in long_df.columns
        or "ticker" not in long_df.columns
    ):
        return pd.DataFrame()

    sub = long_df.dropna(subset=[COL_RETURN])
    if tickers is not None:
        wanted = [t for t in dict.fromkeys(tickers)]  # de-dupe, preserve order
        sub = sub[sub["ticker"].isin(wanted)]
    if sub.empty:
        return pd.DataFrame()

    mat = sub.pivot_table(
        index="date", columns="ticker", values=COL_RETURN, aggfunc="mean"
    )
    mat = mat.loc[sorted(mat.index)]  # chronological rows (ISO date strings)

    if tickers is not None:
        # Preserve the requested holdings order for any ticker that has history.
        cols = [t for t in dict.fromkeys(tickers) if t in mat.columns]
        mat = mat[cols]
    return mat


def correlation_matrix(
    return_matrix: pd.DataFrame, min_overlap: int = 20
) -> pd.DataFrame:
    """Pairwise Pearson correlation of holding return series.

    ``min_overlap`` is the minimum number of overlapping daily-return
    observations required for a pair to get a (non-NaN) correlation — pairs whose
    held windows barely intersect would otherwise report a spuriously precise
    correlation off a handful of points. Tickers with no qualifying pair (every
    off-diagonal cell NaN) are dropped so the rendered matrix carries signal.

    Returns an empty frame when fewer than two tickers have a usable series.
    """
    if return_matrix is None or return_matrix.shape[1] < 2:
        return pd.DataFrame()

    corr = return_matrix.corr(min_periods=max(2, int(min_overlap)))

    # Drop tickers whose only non-NaN entry is their own diagonal (no overlap
    # with any other holding) — they carry no pairwise information.
    off_diag = corr.where(~_eye_mask(corr))
    keep = off_diag.notna().any(axis=1)
    corr = corr.loc[keep, keep]
    if corr.shape[1] < 2:
        return pd.DataFrame()
    return corr


def high_correlation_pairs(
    corr_matrix: pd.DataFrame, threshold: float = 0.8
) -> list[tuple[str, str, float]]:
    """Upper-triangle pairs whose |correlation| ≥ ``threshold``.

    Returns ``(ticker_a, ticker_b, corr)`` tuples sorted by descending absolute
    correlation — the MSFT+AAPL+GOOGL>0.8 surface the issue asks for. Highly
    *negatively* correlated pairs are surfaced too (they matter just as much for
    concentration / hedging), hence the absolute-value comparison.
    """
    if corr_matrix is None or corr_matrix.empty:
        return []

    cols = list(corr_matrix.columns)
    pairs: list[tuple[str, str, float]] = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            val = corr_matrix.iat[i, j]
            if pd.notna(val) and abs(float(val)) >= float(threshold):
                pairs.append((cols[i], cols[j], float(val)))
    pairs.sort(key=lambda p: abs(p[2]), reverse=True)
    return pairs


def _eye_mask(df: pd.DataFrame) -> pd.DataFrame:
    """Boolean frame, True on the diagonal — used to ignore self-correlation."""
    mask = pd.DataFrame(False, index=df.index, columns=df.columns)
    for label in df.index:
        if label in mask.columns:
            mask.at[label, label] = True
    return mask
