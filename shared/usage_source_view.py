"""Pure view-model helpers for the per-source cost breakdown on the API
(LLM Cost) page.

The page's ``load_claude_code_usage`` loader already returns a long-form
``df_model`` per (date, source, model) — but the page's headline section
aggregates across *all* sources, so the groom runs' cost is indistinguishable
from interactive laptop usage. These helpers lift the ``source`` dimension
into a per-source summary (cost, cache-read %, WET, run count proxy) so the
"which source cost what, and how cache-efficient was it" question is
answerable from the cost page rather than only the Backlog Groom page.

Pure over ``df_model`` — no S3, no streamlit — so it is unit-testable with a
hand-built DataFrame, mirroring ``shared/expense_view.py``'s style.
"""
from __future__ import annotations

import pandas as pd

# Sources that are unambiguously autonomous fleet runs (vs interactive
# laptop use). The groom is the canonical member; kept as a set so a new
# autonomous source is additive without touching the helper's call sites.
AUTONOMOUS_SOURCES: frozenset[str] = frozenset({"groom"})


def cache_read_pct(group: pd.DataFrame) -> float | None:
    """Share of raw tokens that were cache reads for *group*.

    Returns ``None`` when ``total`` is zero (no tokens — "not reported", not
    "0%"), matching the Backlog Groom page's ``cache_read_pct`` convention so
    a missing value never renders as a healthy 0%.
    """
    total = float(group["total"].sum()) if not group.empty else 0.0
    if total <= 0:
        return None
    cache_read = float(group["cache_read_input_tokens"].sum()) if "cache_read_input_tokens" in group else 0.0
    return 100.0 * cache_read / total


def source_breakdown(df_model: pd.DataFrame) -> pd.DataFrame:
    """One row per ``source`` with cost, cache-read %, WET, and token totals.

    Columns: source, cost_usd, cache_read_pct, wet, total_tokens,
    fresh_input_tokens, is_autonomous, run_days. Sorted cost-desc so the
    heaviest spender leads. An empty ``df_model`` yields an empty frame
    (the caller renders the "no data" caption).
    """
    if df_model is None or df_model.empty:
        return pd.DataFrame(columns=[
            "source", "cost_usd", "cache_read_pct", "wet", "total_tokens",
            "fresh_input_tokens", "is_autonomous", "run_days",
        ])
    rows = []
    for source, group in df_model.groupby("source"):
        total_tokens = float(group["total"].sum())
        fresh = float(group["input_tokens"].sum()) if "input_tokens" in group else 0.0
        rows.append({
            "source": source,
            "cost_usd": float(group["cost_usd"].sum()),
            "cache_read_pct": cache_read_pct(group),
            "wet": float(group["wet"].sum()) if "wet" in group else 0.0,
            "total_tokens": total_tokens,
            "fresh_input_tokens": fresh,
            "is_autonomous": source in AUTONOMOUS_SOURCES,
            "run_days": int(group["date"].nunique()) if "date" in group else 0,
        })
    out = pd.DataFrame(rows).sort_values("cost_usd", ascending=False, ignore_index=True)
    return out


def daily_cost_by_source(df_model: pd.DataFrame, sources: list[str] | None = None) -> pd.DataFrame:
    """Per-date cost, one column per source, for a stacked/line trend chart.

    *sources* filters (default: all). Returns a wide frame indexed by date with
    a column per source (0.0-filled gaps). Sorted by date ascending.
    """
    if df_model is None or df_model.empty or "date" not in df_model:
        return pd.DataFrame()
    d = df_model if sources is None else df_model[df_model["source"].isin(sources)]
    if d.empty:
        return pd.DataFrame()
    daily = (d.groupby(["date", "source"], as_index=False)["cost_usd"]
             .sum()
             .pivot(index="date", columns="source", values="cost_usd")
             .fillna(0.0)
             .sort_index())
    return daily
