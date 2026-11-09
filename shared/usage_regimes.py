"""Labels for recorded usage pricing; never re-price historical documents."""
from __future__ import annotations

import math

# pandas ships no stubs and pandas-stubs is not in the lock; every other module
# that imports it carries this error in .basedpyright/baseline.json, which a new
# module may not grow. The frame is only read through `.columns` and
# `.itertuples()`, whose declared types are complete, so nothing Unknown leaks
# past the import.
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]

_UNRECORDED = "unrecorded"


def _cell(value: object) -> str:
    """A cell as text; missing values (None / NaN / pd.NA) read as ``unrecorded``."""
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return _UNRECORDED
    return str(value)


def _regime_rows(frame: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(date, cache_ttl, wet_version) per row, in frame order, missing → ``unrecorded``."""
    names = [str(column) for column in frame.columns]
    rows: list[tuple[str, str, str]] = []
    for values in frame.itertuples(index=False, name=None):
        record: dict[str, object] = dict(zip(names, values))
        rows.append((
            _cell(record.get("date")),
            _cell(record.get("cache_ttl")),
            _cell(record.get("wet_version")),
        ))
    return rows


def usage_regime_breaks(frame: pd.DataFrame) -> list[str]:
    """Dates where the observed set of TTL/WET pairs changes."""
    if frame.empty:
        return []
    pairs_by_date: dict[str, set[tuple[str, str]]] = {}
    for date, ttl, version in _regime_rows(frame):
        pairs_by_date.setdefault(date, set()).add((ttl, version))
    previous: set[tuple[str, str]] | None = None
    breaks: list[str] = []
    for date in sorted(pairs_by_date):
        pairs = pairs_by_date[date]
        if previous is not None and pairs != previous:
            breaks.append(date)
        previous = pairs
    return breaks


def usage_regime_caption(frame: pd.DataFrame) -> str:
    """Describe the exact window aggregated by a page, including discontinuities."""
    if frame.empty:
        return "No recorded pricing regime in this window."
    # Distinct (ttl, version) pairs in first-seen order.
    pairs = list(dict.fromkeys((ttl, version) for _date, ttl, version in _regime_rows(frame)))
    labels: list[str] = []
    for ttl, version in pairs:
        label = "unrecorded (TTL unknown; historically priced as 5m)" if ttl == _UNRECORDED else ttl
        labels.append(f"cache TTL {label} / WET {version}")
    caption = "Recorded pricing: " + "; ".join(labels) + "."
    if len(pairs) > 1:
        dates = usage_regime_breaks(frame)
        caption += (
            " Window spans a pricing/WET regime break"
            + (f" (observed changes: {', '.join(dates)})" if dates else " (overlapping regimes)")
            + ". Historical discontinuities: 2026-09-19 WET v2 dedupe fix; "
            "2026-09-22 WET v3 / cache-TTL change. Totals retain recorded pricing; "
            "history has not been normalized."
        )
    return caption
