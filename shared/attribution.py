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

Contribution basis (config#1211): the snapshot's ``alpha_contribution_pct`` is
NAV-weighted on **today's** NAV. The EOD Report page ties to the headline daily
alpha on a **prior-NAV** basis. We rebase to prior-NAV here so the two console
surfaces report identical per-position bps:

    contrib_usd       = alpha_contribution_pct/100 × today_nav
    contrib_bps_prior = contrib_usd / prior_nav × 10000
                      = alpha_contribution_pct × 100 × (today_nav / prior_nav)

``prior_nav`` is the previous trading day's ``portfolio_nav`` from the eod_pnl
history. On the first available day (no prior NAV) we fall back to today's NAV
(ratio 1) — the original today's-NAV reading.

Weekly rollup: the return-domain series (total return, market-relative alpha)
compound **geometrically** over the days held in the week; the contribution
series sums **additively** — daily contributions are additive by construction
(they sum to the portfolio's alpha), so geometric compounding would be
incorrect there.

Pre-attribution backfill (config#1212): snapshots carry per-position
``daily_return_pct`` / ``alpha_contribution_pct`` only from 2026-04-20 onward.
Earlier rows (back to 2026-03-09) instead carry only a per-position
``closing_price`` (plus ``market_value`` / ``sector``). For those rows we
**reconstruct the price-relative lens read-side**: ``daily_return_pct`` is the
day-over-day pct change of the ticker's ``closing_price`` versus the most recent
prior trading day it was held, and ``market_relative_pct`` is that minus the
row's ``spy_return_pct`` — the *identical* formula used for the stored post-4/20
rows, so the two windows are comparable. This lights up the RETURN and
market-relative ALPHA heatmaps back to the start of live history.

HONESTY / known limitation: the NAV-weighted **contribution (bps)** lens needs
per-position ``alpha_contribution_pct`` (per-name dollar-alpha), which is *not*
reconstructable from ``closing_price`` alone. We therefore leave
``alpha_contrib_bps`` as ``None`` for reconstructed pre-4/20 rows rather than
fabricate it — ``to_matrix`` drops the NaN cells, so the contribution heatmap
simply shows gaps for those dates while the return lenses fill in. Post-4/20
behavior is unchanged: rows that already carry ``daily_return_pct`` keep using
the stored value and full (return + contribution) attribution.
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


# Per-position price field in pre-attribution (pre-2026-04-20) snapshots, used
# to reconstruct ``daily_return_pct`` for rows that predate the stored value.
# ``closing_price`` is the canonical name (config#1212); the aliases are
# tolerated defensively in case the producer used a different key.
_CLOSE_KEYS = ("closing_price", "close_price", "close")


def _coerce_float(v):
    """Return ``v`` as a float, or None for None / NaN / unparseable."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if pd.notna(f) else None


def _closing_price(pos: dict) -> float | None:
    """Return a per-position closing price from a snapshot struct, or None.

    Looks up the canonical ``closing_price`` field (with defensive aliases) used
    by pre-attribution snapshots to reconstruct day-over-day returns."""
    for key in _CLOSE_KEYS:
        if key in pos:
            px = _coerce_float(pos.get(key))
            if px is not None and px > 0:
                return px
    return None


def _iso_week_monday(date_str: str) -> str:
    """ISO-week Monday (YYYY-MM-DD) for ``date_str`` — a stable per-week label
    shared across all tickers (so a partial week doesn't fragment into separate
    columns by each ticker's last-held day)."""
    iso = date.fromisoformat(date_str).isocalendar()
    return date.fromisocalendar(iso[0], iso[1], 1).isoformat()


def _prior_nav_by_date(eod_pnl: pd.DataFrame) -> dict[str, float | None]:
    """Map each date → the previous trading day's ``portfolio_nav``.

    Built from the distinct (date, NAV) pairs in ``eod_pnl``, ordered by date.
    The earliest date maps to None (no prior NAV). Used to rebase the
    contribution lens onto the prior-NAV basis (config#1211)."""
    if "portfolio_nav" not in eod_pnl.columns or "date" not in eod_pnl.columns:
        return {}
    nav_by_date: dict[str, float | None] = {}
    for _, row in eod_pnl.iterrows():
        d = str(row.get("date"))
        if d not in nav_by_date:
            nav_by_date[d] = _coerce_float(row.get("portfolio_nav"))
    ordered = sorted(nav_by_date)
    prior: dict[str, float | None] = {}
    for i, d in enumerate(ordered):
        prior[d] = nav_by_date[ordered[i - 1]] if i > 0 else None
    return prior


def build_long_frame(eod_pnl: pd.DataFrame | None) -> pd.DataFrame:
    """Explode ``eod_pnl`` rows into one row per (date, ticker).

    Columns: ``date, ticker, sector, daily_return_pct, market_relative_pct,
    alpha_contrib_bps, weight, spy_return_pct``.

    Rows whose snapshot carries a per-ticker ``daily_return_pct`` (2026-04-20
    onward) use the stored value and get full attribution, with
    ``alpha_contrib_bps`` rebased to the prior-NAV basis (config#1211) to tie out
    with the EOD Report page.

    Pre-attribution rows (before 2026-04-20) lack ``daily_return_pct`` but carry
    a per-position ``closing_price``; for those we reconstruct the price-relative
    lens (config#1212): ``daily_return_pct`` from the ticker's day-over-day
    ``closing_price`` change vs the most recent prior day it was held, and
    ``market_relative_pct`` as that minus ``spy_return_pct`` — the same formula
    used for the stored rows. The NAV-weighted contribution lens is *not*
    reconstructable from price alone, so ``alpha_contrib_bps`` is left None for
    these rows (``to_matrix`` drops the NaN cells → the contribution heatmap
    shows gaps pre-4/20 while the return lenses fill in). A position with neither
    a stored ``daily_return_pct`` nor a usable prior ``closing_price`` (e.g. its
    first-ever held day, or a missing/zero price) is skipped.

    Returns an empty frame (with the right columns) when there is nothing to
    show.
    """
    if (
        eod_pnl is None
        or eod_pnl.empty
        or "positions_snapshot" not in eod_pnl.columns
    ):
        return pd.DataFrame(columns=_LONG_COLS)

    prior_nav_by_date = _prior_nav_by_date(eod_pnl)

    # Reconstruction of pre-attribution returns needs each ticker's most recent
    # prior closing price, so process rows in chronological (date) order and
    # carry the last-seen close forward per ticker. We sort a positional view
    # rather than mutating the caller's frame; ties keep input order (stable).
    if "date" in eod_pnl.columns:
        ordered_eod = eod_pnl.sort_values("date", kind="stable")
    else:
        ordered_eod = eod_pnl
    last_close_by_ticker: dict[str, float] = {}

    records: list[dict] = []
    for _, row in ordered_eod.iterrows():
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
        prior_nav = prior_nav_by_date.get(day)
        # Rebase today's-NAV contribution onto the prior-NAV basis. Fall back to
        # ratio 1 when prior NAV is unavailable (first day / missing NAV).
        nav_ratio = (
            (nav / prior_nav)
            if (nav is not None and prior_nav) else 1.0
        )
        for ticker, pos in snap.items():
            if not isinstance(pos, dict):
                continue
            tkr = str(ticker)
            mv = _coerce_float(pos.get("market_value"))
            close = _closing_price(pos)

            dr = _coerce_float(pos.get("daily_return_pct"))
            if dr is not None:
                # Post-attribution row: stored return + full contribution lens.
                contrib_pct = _coerce_float(pos.get("alpha_contribution_pct"))
                contrib_bps = (
                    contrib_pct * 100.0 * nav_ratio
                    if contrib_pct is not None else None
                )
            else:
                # Pre-attribution row: reconstruct the price-relative lens only
                # (config#1212). Need a prior held close for this ticker; if we
                # don't have one (or no usable close today) there's nothing to
                # plot, so skip — matching the original pre-attribution behavior.
                prior_close = last_close_by_ticker.get(tkr)
                if close is None or prior_close is None:
                    # Carry today's close forward (if any) so the next day this
                    # ticker is held can chain off it, then skip — there's no
                    # return to plot for this row.
                    if close is not None:
                        last_close_by_ticker[tkr] = close
                    continue
                dr = (close / prior_close - 1.0) * 100.0
                # Contribution lens is not reconstructable from price alone.
                contrib_bps = None

            # Carry the most recent observed close forward per ticker, so a
            # later pre-attribution row can reconstruct its return against it.
            if close is not None:
                last_close_by_ticker[tkr] = close

            records.append({
                "date": day,
                "ticker": tkr,
                "sector": pos.get("sector"),
                COL_RETURN: dr,
                COL_RELATIVE: (dr - spy_ret) if spy_ret is not None else None,
                COL_CONTRIB_BPS: contrib_bps,
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
