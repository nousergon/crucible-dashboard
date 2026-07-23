"""Pure view-model helpers for the Expenses page (``views/50_Expenses.py``).

Kept out of the view script so the formatting/derivation logic is unit-testable
without exec'ing Streamlit (mirrors how ``results/view_model.py`` backs the
Crucible Results views). Input is the expense-collector Lambda's rollup doc
(``expenses/latest.json``, schema_version 1 — producer: alpha-engine-data
``infrastructure/lambdas/expense-collector/index.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone

PACE_BADGES = {
    "over": "🔴 over",
    "under": "🟢 under",
    "fixed": "💳 fixed",
}

STATUS_BADGES = {
    "error": "⚠️ error",
    "not_configured": "⚙️ not configured",
}

# Month-close reconciliation (alpha-engine-config#2849) — |delta_pct| beyond
# this is visible drift, not rounding/timing noise. Kept in lockstep with the
# collector's own RECONCILIATION_DELTA_PCT_THRESHOLD (alpha-engine-data
# infrastructure/lambdas/expense-collector/index.py) — both sides of a
# producer/console pair must agree on what counts as "flagged" or a row the
# collector flags could render unhighlighted here (or vice versa).
RECONCILIATION_DELTA_PCT_THRESHOLD = 0.08

RECONCILIATION_STATUS_BADGES = {
    "error": "⚠️ error",
    "not_configured": "⚙️ not configured",
    "not_available": "— not available",
}


def usd(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def quota_str(quota: dict | None) -> str:
    """``used/limit unit`` (limit "?" when no quota figure is configured)."""
    if not quota:
        return "—"
    limit = quota.get("limit")
    limit_s = f"{limit:,.0f}" if isinstance(limit, (int, float)) else "?"
    used = quota.get("used") or 0
    return f"{used:,.0f}/{limit_s} {quota.get('unit', '')}".strip()


def pace_badge(row: dict) -> str:
    """One glanceable cell: provider health first (error/not-configured),
    then budget/quota pacing, then em-dash when no budget is set."""
    if row.get("status") in STATUS_BADGES:
        return STATUS_BADGES[row["status"]]
    return PACE_BADGES.get(row.get("pace") or "", "—")


def provider_table_rows(doc: dict) -> list[dict]:
    """Flatten the rollup into display rows, spend-heaviest first (rows with
    no MTD figure — errors/not-configured — sink to the bottom so the money
    ranking stays readable; their badges keep them visible)."""
    rows = []
    for p in doc.get("providers", []):
        rows.append({
            "Provider": p.get("label", p.get("key", "?")),
            "MTD": usd(p.get("mtd_cost_usd")),
            "Projected": usd(p.get("projected_month_end_usd")),
            "Budget": usd(p.get("budget_usd")),
            "Quota": quota_str(p.get("quota")),
            "Pace": pace_badge(p),
            "Note": p.get("note") or p.get("error") or "",
            "_mtd_sort": p.get("mtd_cost_usd") if p.get("mtd_cost_usd") is not None else -1.0,
        })
    rows.sort(key=lambda r: -r["_mtd_sort"])
    for r in rows:
        del r["_mtd_sort"]
    return rows


def error_rows(doc: dict) -> list[dict]:
    return [p for p in doc.get("providers", []) if p.get("status") == "error"]


def reconciliation_table_rows(doc: dict) -> list[dict]:
    """Flatten one period's ``expenses/reconciliation/{period}.json`` into
    display rows — one per provider, worst-drift first (rows with no
    computable ``delta_pct`` — errors/not-available — sink to the bottom,
    same convention as ``provider_table_rows``). A ``⚠️`` prefix on
    ``Delta %`` marks rows past ``RECONCILIATION_DELTA_PCT_THRESHOLD`` so the
    highlight survives a plain ``st.dataframe`` render (this page uses no
    Styler/column_config coloring anywhere else — see ``pace_badge``)."""
    rows = []
    for key, r in (doc.get("providers") or {}).items():
        delta_pct = r.get("delta_pct")
        flagged = delta_pct is not None and abs(delta_pct) > RECONCILIATION_DELTA_PCT_THRESHOLD
        delta_pct_s = "—" if delta_pct is None else f"{delta_pct * 100:+,.1f}%"
        rows.append({
            "Provider": key,
            "Projected (last seen)": usd(r.get("projected_last_seen")),
            "Accrued MTD (final run)": usd(r.get("accrued_mtd_final")),
            "Actual (final)": usd(r.get("actual_final")),
            "Delta $": usd(r.get("delta_usd")),
            "Delta %": (f"⚠️ {delta_pct_s}" if flagged else delta_pct_s),
            "Status": RECONCILIATION_STATUS_BADGES.get(r.get("status"), "✅ ok"),
            "Note": r.get("note") or "",
            "_abs_delta_pct": abs(delta_pct) if delta_pct is not None else -1.0,
        })
    rows.sort(key=lambda r: -r["_abs_delta_pct"])
    for r in rows:
        del r["_abs_delta_pct"]
    return rows


def as_of_age_hours(doc: dict, now: datetime | None = None) -> float | None:
    """Hours since the collector last wrote the rollup (None if unparseable —
    caller renders that as its own staleness warning, never silently)."""
    try:
        as_of = datetime.fromisoformat(doc["as_of"])
    except Exception:  # noqa: BLE001 — malformed as_of surfaces as "unknown age" upstream
        return None
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - as_of).total_seconds() / 3600.0
