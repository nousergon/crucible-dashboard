"""Optimizer target-weight time-series matrix for the order-book rationale view.

Pure data transform — pivots the per-ticker ``optimizer.target_weight``
field across a window of order-book rationale artifacts (one per trading
day) into a ``Ticker × trading-day`` matrix of target % of NAV. Consumed
by ``views/16_Order_Book_Rationale.py`` to answer "how are the optimizer
holdings targets drifting over time?". Extracted to ``shared/`` so the
pivot/filter math is unit-testable without importing the Streamlit page
module (mirrors ``shared/reconciliation.py``).

Source field: ``tickers[].optimizer.target_weight`` (fraction of NAV),
produced by ``alpha-engine`` schema ≥ 1.1.0 on each daily rationale
artifact. The window is whatever ``load_order_book_rationale_history``
returns (≤14 days today). No producer-side change is needed — the data
already lives in the artifacts the page loads.

Cell semantics (load-bearing — do NOT collapse to 0):
  * A float is the ticker's target % of NAV that day.
  * ``NaN`` means the ticker carried **no optimizer target** that day
    (not in the considered universe, or a pre-optimizer artifact) — this
    is deliberately distinct from a real ``0.00%`` target the optimizer
    actively chose for an in-universe name.
"""
from __future__ import annotations

import pandas as pd


def _day_key(payload: dict) -> str | None:
    """Trading-day key for a rationale payload (calendar fallback)."""
    return payload.get("trading_day") or payload.get("calendar_date")


def _is_book_or_forming(rec: dict) -> bool:
    """A-filter: held positions + approved entries (the actual/forming book).

    ``held`` covers currently-owned names (including those being reduced
    or exited — they are still held); ``approved_entry`` is the forming
    book not yet owned. Everything else (considered-but-not-entered,
    vetoed, risk-blocked, optimizer-zero) is excluded from the A view.
    """
    return bool(rec.get("held")) or rec.get("terminal_state") == "approved_entry"


def build_target_weight_matrix(
    history: list[dict],
    *,
    held_only: bool = True,
) -> pd.DataFrame:
    """Pivot optimizer target weights into a ``Ticker × trading-day`` matrix.

    Args:
        history: order-book rationale payloads (any order). Each is one
            trading day's artifact carrying ``tickers[].optimizer.target_weight``.
        held_only: when True (option A) keep only tickers that were held
            or an approved entry on at least one day in the window — the
            actual/forming book. When False (option B) keep every ticker
            that carried an optimizer target on any day — the full
            considered universe.

    Returns:
        DataFrame indexed by ticker (rows sorted by the latest day's
        target descending, NaN last, mean as tiebreak), columns are
        trading-day keys ascending (oldest → newest), values are target
        % of NAV (``target_weight * 100``) with ``NaN`` for days a ticker
        had no optimizer target. Empty DataFrame when the window has no
        usable target weights.
    """
    # Dedup to one artifact per trading day — a same-day re-run would
    # otherwise produce duplicate columns. Latest run_id wins.
    by_day: dict[str, dict] = {}
    for payload in history or []:
        if not isinstance(payload, dict):
            continue
        day = _day_key(payload)
        if not day:
            continue
        prior = by_day.get(day)
        if prior is None or str(payload.get("run_id") or "") >= str(
            prior.get("run_id") or ""
        ):
            by_day[day] = payload

    # First pass: per-day {ticker: target_pct} maps + the kept-ticker set.
    per_day: dict[str, dict[str, float]] = {}
    keep: set[str] = set()
    for day, payload in by_day.items():
        day_map: dict[str, float] = {}
        for rec in payload.get("tickers") or []:
            if not isinstance(rec, dict):
                continue
            ticker = rec.get("ticker")
            if not ticker:
                continue
            opt = rec.get("optimizer") or {}
            tw = opt.get("target_weight")
            has_target = isinstance(tw, (int, float))
            if has_target:
                day_map[ticker] = float(tw) * 100.0
            if held_only:
                if _is_book_or_forming(rec):
                    keep.add(ticker)
            elif has_target:
                keep.add(ticker)
        per_day[day] = day_map

    if not keep:
        return pd.DataFrame()

    days_sorted = sorted(per_day.keys())  # oldest → newest
    data = {
        day: {tk: per_day[day].get(tk, float("nan")) for tk in keep}
        for day in days_sorted
    }
    df = pd.DataFrame(data, index=sorted(keep), columns=days_sorted)

    # Sort rows by the latest day's target (desc, NaN last), tiebreak by
    # mean target across the window — biggest current bets on top.
    latest = days_sorted[-1]
    order = df.assign(_latest=df[latest], _mean=df.mean(axis=1, skipna=True))
    order = order.sort_values(
        ["_latest", "_mean"], ascending=[False, False], na_position="last"
    )
    return df.loc[order.index]
