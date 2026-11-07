"""Labels for recorded usage pricing; never re-price historical documents."""
from __future__ import annotations

import pandas as pd


def usage_regime_breaks(frame: pd.DataFrame) -> list[str]:
    """Dates where the observed set of TTL/WET pairs changes."""
    previous: set[tuple[str, str]] | None = None
    breaks: list[str] = []
    if frame.empty:
        return breaks
    for date, rows in frame.fillna("unrecorded").groupby("date"):
        pairs = set(zip(rows["cache_ttl"].astype(str), rows["wet_version"].astype(str)))
        if previous is not None and pairs != previous:
            breaks.append(str(date))
        previous = pairs
    return breaks


def usage_regime_caption(frame: pd.DataFrame) -> str:
    """Describe the exact window aggregated by a page, including discontinuities."""
    if frame.empty:
        return "No recorded pricing regime in this window."
    pairs = frame[["cache_ttl", "wet_version"]].fillna("unrecorded").drop_duplicates()
    labels: list[str] = []
    for ttl, version in pairs.itertuples(index=False, name=None):
        label = "unrecorded (TTL unknown; historically priced as 5m)" if ttl == "unrecorded" else str(ttl)
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
