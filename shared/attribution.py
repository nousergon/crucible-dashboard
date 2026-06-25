"""Position-level attribution history → (security × period) heatmap matrices.

Pure data-shaping over the executor's EOD history (``trades/eod_pnl.csv``,
loaded by ``loaders.s3_loader.load_eod_pnl``). Each ``eod_pnl`` row carries a
JSON ``positions_snapshot`` mapping ticker → the per-position EOD struct written
by alpha-engine ``executor/eod_reconcile.py``, including:

  - ``daily_return_pct``       single-day price return (today vs prior close)
  - ``alpha_contribution_pct`` NAV-weighted daily alpha contribution (%)
  - ``market_value`` / ``sector``

These functions reshape that history into (security × period) matrices for the
Attribution Heatmaps console page (``views/37_Attribution_Heatmaps.py``). No S3
I/O here — fully unit-testable.

Two alpha lenses (the page toggles between them):
  - **market-relative** — raw ``position_return - SPY_return``, unweighted;
    comparable across names regardless of position size.
  - **contribution** — NAV-weighted contribution in bps; sums across names to
    the portfolio's daily alpha (the EOD Report convention).

Weekly rollup: the return-domain series (total return, market-relative alpha)
compound **geometrically** over the days held in the week; the contribution
series sums **additively** — daily contributions are additive by construction
(they sum to the portfolio's alpha), so geometric compounding would be
incorrect there.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

# Canonical metric column names in the long / weekly frames.
COL_RETURN = "daily_return_pct"        # total return (%)
COL_RELATIVE = "market_relative_pct"   # position return - SPY return (%)
COL_CONTRIB_BPS = "alpha_contrib_bps"  # NAV-weighted contribution (bps)

_LONG_COLS = [
    "date", "ticker", "sector", COL_RETURN, COL_RELATIVE,
    COL_CONTRIB_BPS, "weight", "spy_return_pct",
]
_WEEKLY_COLS = [
    "week", "week_start", "ticker", "sector", COL_RETURN,
    COL_RELATIVE, COL_CONTRIB_BPS, "n_days",
]


def _coerce_float(v):
    """Return ``v`` as a float, or None for None / NaN / unparseable."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if pd.notna(f) else None


def _iso_week_monday(date_str: str) -> str:
    """ISO-week Monday (YYYY-MM-DD) for ``date_str`` — a stable per-week label
    shared across all tickers (so a partial week doesn't fragment into separate
    columns by each ticker's last-held day)."""
    iso = date.fromisoformat(date_str).isocalendar()
    return date.fromisocalendar(iso[0], iso[1], 1).isoformat()


def build_long_frame(eod_pnl: pd.DataFrame | None) -> pd.DataFrame:
    """Explode ``eod_pnl`` rows into one row per (date, ticker).

    Columns: ``date, ticker, sector, daily_return_pct, market_relative_pct,
    alpha_contrib_bps, weight, spy_return_pct``. Only positions whose snapshot
    carries a per-ticker ``daily_return_pct`` are included — the pre-attribution
    rows (before 2026-04-20) have snapshots without per-position returns and are
    skipped. Returns an empty frame (with the right columns) when there is
    nothing to show.
    """
    if (
        eod_pnl is None
        or eod_pnl.empty
        or "positions_snapshot" not in eod_pnl.columns
    ):
        return pd.DataFrame(columns=_LONG_COLS)

    records: list[dict] = []
    for _, row in eod_pnl.iterrows():
        raw = row.get("positions_snapshot")
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            snap = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(snap, dict):
            continue
        day = str(row.get("date"))
        spy_ret = _coerce_float(row.get("spy_return_pct"))
        nav = _coerce_float(row.get("portfolio_nav"))
        for ticker, pos in snap.items():
            if not isinstance(pos, dict):
                continue
            dr = _coerce_float(pos.get("daily_return_pct"))
            if dr is None:
                continue  # pre-attribution row — nothing to plot
            contrib_pct = _coerce_float(pos.get("alpha_contribution_pct"))
            mv = _coerce_float(pos.get("market_value"))
            records.append({
                "date": day,
                "ticker": str(ticker),
                "sector": pos.get("sector"),
                COL_RETURN: dr,
                COL_RELATIVE: (dr - spy_ret) if spy_ret is not None else None,
                COL_CONTRIB_BPS: (
                    contrib_pct * 100.0 if contrib_pct is not None else None
                ),
                "weight": (mv / nav) if (mv is not None and nav) else None,
                "spy_return_pct": spy_ret,
            })

    df = pd.DataFrame.from_records(records, columns=_LONG_COLS)
    if not df.empty:
        df = df.sort_values(["date", "ticker"]).reset_index(drop=True)
    return df


def build_weekly_frame(long_df: pd.DataFrame) -> pd.DataFrame:
    """Roll the daily long frame up to one row per (ISO week, ticker).

    Columns: ``week`` (label = ISO-week Monday, stable across tickers),
    ``week_start`` (== ``week``, for chronological sort), ``ticker``,
    ``sector``, ``daily_return_pct`` (geometric weekly total return %),
    ``market_relative_pct`` (geometric: compounded position return − compounded
    SPY over the days held that week), ``alpha_contrib_bps`` (additive sum of
    daily contributions over the week), ``n_days``.
    """
    if long_df is None or long_df.empty:
        return pd.DataFrame(columns=_WEEKLY_COLS)

    work = long_df.copy()
    work["_wk"] = work["date"].map(_iso_week_monday)

    records: list[dict] = []
    for (wk, ticker), g in work.groupby(["_wk", "ticker"], sort=True):
        g = g.sort_values("date")

        dr = g[COL_RETURN].dropna()
        total_ret = (
            (float((1.0 + dr / 100.0).prod()) - 1.0) * 100.0 if len(dr) else None
        )

        # Market-relative: compound position vs compound SPY over the SAME held
        # days (apples-to-apples within a partial week).
        gg = g.dropna(subset=[COL_RETURN, "spy_return_pct"])
        if len(gg):
            comp_pos = float((1.0 + gg[COL_RETURN] / 100.0).prod())
            comp_spy = float((1.0 + gg["spy_return_pct"] / 100.0).prod())
            rel = (comp_pos - comp_spy) * 100.0
        else:
            rel = None

        contrib = g[COL_CONTRIB_BPS].dropna()
        contrib_bps = float(contrib.sum()) if len(contrib) else None

        records.append({
            "week": wk,
            "week_start": wk,
            "ticker": str(ticker),
            "sector": g["sector"].iloc[-1],
            COL_RETURN: total_ret,
            COL_RELATIVE: rel,
            COL_CONTRIB_BPS: contrib_bps,
            "n_days": int(len(g)),
        })

    df = pd.DataFrame.from_records(records, columns=_WEEKLY_COLS)
    if not df.empty:
        df = df.sort_values(["week_start", "ticker"]).reset_index(drop=True)
    return df


def to_matrix(
    frame: pd.DataFrame, value_col: str, period_col: str = "date"
) -> pd.DataFrame:
    """Pivot a long / weekly frame into a (ticker × period) matrix.

    Rows = tickers, ordered best-to-worst by the summed value over the window
    (best performer on top); columns = period in chronological order. Cells hold
    the metric; NaN where the ticker was not held in that period (rendered as a
    gap). Returns an empty frame if the metric is entirely absent.
    """
    if frame is None or frame.empty or value_col not in frame.columns:
        return pd.DataFrame()
    sub = frame.dropna(subset=[value_col])
    if sub.empty:
        return pd.DataFrame()
    mat = sub.pivot_table(
        index="ticker", columns=period_col, values=value_col, aggfunc="mean"
    )
    mat = mat[sorted(mat.columns)]  # chronological columns (ISO date strings)
    order = mat.sum(axis=1, skipna=True).sort_values(ascending=False).index
    return mat.loc[order]
