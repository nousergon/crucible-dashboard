"""Pure helpers for the Backlog Groom console page — dispatcher-decision
table rows, cross-run trend frames, trailing-window KPIs, and the
disposition-audit summary.

Split out of ``views/42_Backlog_Groom.py`` so every transformation the page
renders is unit-testable without a Streamlit runtime (same pattern as
``loaders/groom_efficiency.py``). Inputs are the raw S3 documents the
``s3_loader`` groom loaders return; outputs are plain dicts/lists ready for
``pd.DataFrame`` / ``st.metric``.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

#: Fixed categorical color per complexity tier (never cycled/reassigned —
#: a chart filtered to fewer tiers must not repaint the survivors).
TIER_COLOR: dict[str, str] = {
    "low": "#2a78d6",
    "mid": "#1baf7a",
    "high": "#eda100",
}
TIER_ORDER: tuple[str, ...] = ("low", "mid", "high")

_SLOT_TIME_RE = re.compile(r"^trigger-(\d{2})(\d{2})$")
#: Grace period after a scheduled slot's UTC time before a missing decision
#: record is flagged — covers dispatcher cold-start + S3 write latency.
_MISSING_GRACE = timedelta(minutes=30)


def slot_utc_time(slot: str) -> tuple[int, int] | None:
    """``trigger-1900`` -> ``(19, 0)``; None for ad-hoc slot names."""
    m = _SLOT_TIME_RE.match(slot)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return hh, mm


def is_scheduled_slot(slot: str, known_slots: tuple[str, ...] | list[str]) -> bool:
    """Only the canonical dispatcher trio counts as scheduled. Ad-hoc manual
    triggers (e.g. ``trigger-1404`` from a hand-run pace-gate test) must NOT
    join the known-slots set — before this split they spawned phantom
    "missing record" warnings for every other day in the window.
    """
    return slot in tuple(known_slots)


def _decision_boxes_summary(boxes: list[dict]) -> tuple[str, str]:
    """(launched, deferred) compact strings for one decision record."""
    launched: list[str] = []
    deferred: list[str] = []
    for b in boxes:
        filt = b.get("issue_filter") or "+".join(b.get("tiers") or []) or "?"
        if b.get("launch"):
            model = str(b.get("model") or "?").removeprefix("claude-")
            launched.append(f"{filt} → {model}")
        else:
            tiers = "+".join(b.get("tiers") or []) or filt
            deferred.append(f"{tiers}: {b.get('reason') or 'no reason recorded'}")
    return " · ".join(launched), " · ".join(deferred)


def _parse_decided_at(raw: dict) -> datetime | None:
    val = raw.get("decided_at")
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def decision_table_rows(
    records: list[tuple[str, dict, list[dict]]],
    *,
    known_slots: tuple[str, ...] | list[str],
    now: datetime,
    days: int,
) -> list[dict[str, Any]]:
    """One row per decision record in the window, plus one ⚠️ row per
    scheduled slot that is DUE (its UTC time + grace has passed) but has no
    record — the broken-scheduler signal (config#1935), now time-aware so a
    slot later today is not falsely flagged.

    *records* is ``[(key, raw_record, normalized_boxes), ...]``; rows come
    back newest-first with keys: When (UTC), Slot, Type, Status,
    low/mid/high (actionable counts), Launched, Deferred.
    """
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []

    for key, raw, boxes in records:
        m = re.match(r"^groom/decisions/(\d{4}-\d{2}-\d{2})/([^/]+)\.json$", key)
        if not m:
            continue
        date_str, slot = m.group(1), m.group(2)
        seen.add((date_str, slot))
        counts = raw.get("counts") or {}
        launched, deferred = _decision_boxes_summary(boxes)
        n_launched = sum(1 for b in boxes if b.get("launch"))
        skip_reason = raw.get("skip_reason")
        if skip_reason:
            # config-I2540: the dispatcher ran but never reached a launch
            # decision (e.g. demand_all_failed — GitHub enumeration broke).
            # Distinct from a demand-based full skip AND from a missing
            # record: the scheduler DID invoke the Lambda.
            status = f"🔴 {skip_reason}"
            deferred = deferred or str(raw.get("error") or "—")
        elif boxes and n_launched:
            status = f"🟢 launched {n_launched}"
        elif boxes:
            status = "⚪ all deferred"
        else:
            status = "⚪ full skip"
        decided = _parse_decided_at(raw)
        rows.append({
            "When (UTC)": (decided.strftime("%Y-%m-%d %H:%M") if decided
                           else f"{date_str} {slot.removeprefix('trigger-')}"),
            "Slot": slot,
            "Type": ("scheduled" if is_scheduled_slot(slot, known_slots)
                     else "🔧 manual"),
            "Status": status,
            "low": counts.get("low"),
            "mid": counts.get("mid"),
            "high": counts.get("high"),
            "Launched": launched or "—",
            "Deferred": deferred or "—",
            "_sort": (decided or datetime.min.replace(tzinfo=timezone.utc)),
        })

    # Missing-record rows: scheduled slots only, and only once DUE.
    for i in range(days):
        day = (now - timedelta(days=i)).date()
        for slot in known_slots:
            if (day.isoformat(), slot) in seen:
                continue
            hhmm = slot_utc_time(slot)
            if hhmm is None:
                continue
            due_at = datetime(day.year, day.month, day.day, hhmm[0], hhmm[1],
                              tzinfo=timezone.utc) + _MISSING_GRACE
            if now < due_at:
                continue  # not due yet — never flag the future
            rows.append({
                "When (UTC)": f"{day.isoformat()} {hhmm[0]:02d}:{hhmm[1]:02d}",
                "Slot": slot,
                "Type": "scheduled",
                "Status": "⚠️ NO RECORD",
                "low": None, "mid": None, "high": None,
                "Launched": "—",
                "Deferred": "dispatcher never wrote a decision record — "
                            "check the scheduled-groom-dispatcher Lambda",
                "_sort": due_at,
            })

    rows.sort(key=lambda r: r["_sort"], reverse=True)
    for r in rows:
        r.pop("_sort", None)
    return rows


def demand_trend_rows(
    records: list[tuple[str, dict]],
) -> list[dict[str, Any]]:
    """Per-decision actionable-backlog counts by tier, oldest-first —
    the "is the backlog draining?" series. *records* is ``[(key, raw), ...]``.
    """
    out: list[dict[str, Any]] = []
    for _key, raw in records:
        decided = _parse_decided_at(raw)
        counts = raw.get("counts") or {}
        if decided is None or not counts:
            continue
        out.append({
            "decided_at": decided,
            **{t: counts.get(t) for t in TIER_ORDER},
        })
    out.sort(key=lambda r: r["decided_at"])
    return out


def _run_tier(run: dict[str, Any]) -> str:
    """Dominant tier bucket for coloring: ``mid+low`` anchors on mid."""
    filt = str(run.get("issue_filter") or "")
    for tier in ("high", "mid", "low"):
        if tier in filt:
            return tier
    return "mid"


def runs_trend_rows(
    runs: list[tuple[str, dict, dict]],
) -> list[dict[str, Any]]:
    """One row per COVERAGE run (sweeps excluded — no issue queue),
    oldest-first. *runs* is ``[(key, run_doc, efficiency_dict), ...]`` where
    the efficiency dict is ``groom_efficiency.compute_efficiency`` output.
    """
    from loaders.groom_efficiency import parse_run_start

    rows: list[dict[str, Any]] = []
    for key, run, eff in runs:
        if (run.get("run_kind") or "coverage") != "coverage":
            continue
        start = parse_run_start(run)
        total = int(run.get("total_issues") or 0)
        engaged = int(eff.get("engaged") or run.get("engaged") or 0)
        wpe = eff.get("wet_per_engaged")
        rows.append({
            "key": key,
            "run_start": start,
            "tier": _run_tier(run),
            "engaged": engaged,
            "queued": total,
            "coverage_pct": (100.0 * engaged / total) if total else None,
            "undispositioned": int(run.get("undispositioned") or 0),
            "dropped_at_cap": int(run.get("dropped_at_cap") or 0),
            "gated_excluded": int(run.get("gated_excluded") or 0),
            "max_turns_chunks": int(run.get("max_turns_chunks") or 0),
            "closed": sum(1 for i in (run.get("issues") or [])
                          if i.get("disposition") == "closed"),
            "prs": sum(1 for i in (run.get("issues") or [])
                       if i.get("disposition") == "pr_opened"),
            "wet_per_engaged_k": (wpe / 1e3) if wpe is not None else None,
            "floor_fail": bool(run.get("floor_fail")),
        })
    rows.sort(key=lambda r: r["run_start"] or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def window_kpis(
    trend_rows: list[dict[str, Any]],
    *,
    now: datetime,
    days: int = 7,
) -> dict[str, Any]:
    """Trailing-window health headline over coverage runs."""
    cutoff = now - timedelta(days=days)
    window = [r for r in trend_rows
              if r.get("run_start") and r["run_start"] >= cutoff]
    wpes = [r["wet_per_engaged_k"] for r in window
            if r.get("wet_per_engaged_k") is not None]
    return {
        "days": days,
        "runs": len(window),
        "engaged": sum(r["engaged"] for r in window),
        "queued": sum(r["queued"] for r in window),
        "closed": sum(r["closed"] for r in window),
        "prs": sum(r["prs"] for r in window),
        "undispositioned": sum(r["undispositioned"] for r in window),
        "floor_breaches": sum(1 for r in window if r["floor_fail"]),
        "max_turns_chunks": sum(r["max_turns_chunks"] for r in window),
        "median_wet_per_engaged_k": (statistics.median(wpes) if wpes else None),
    }


#: An audit older than this is stale — the weekly cadence self-heals daily
#: (groom_disposition_audit.py), so >8 days means the low-tier box that
#: carries it has not completed an audit pass in over a week.
_AUDIT_STALE_DAYS = 8


def audit_summary(
    key: str | None,
    doc: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Latest disposition-quality audit (config#2153) rolled up for a tile.

    status: ``ok`` | ``fail`` (>=2 FAILs — the page threshold) |
    ``warn`` (1 FAIL or any ERROR) | ``stale`` | ``missing``.
    """
    if not key or not isinstance(doc, dict):
        return {"status": "missing", "date": None, "detail":
                "no groom/audit/{date}.json artifact found — the weekly "
                "disposition-quality audit has never run or S3 listing failed"}
    date_str = doc.get("date") or key.removeprefix("groom/audit/").removesuffix(".json")
    fails = int(doc.get("fail_count") or 0)
    errors = int(doc.get("error_count") or 0)
    passes = int(doc.get("pass_count") or 0)
    try:
        audit_date = datetime.strptime(str(date_str), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (now - audit_date).days
    except ValueError:
        age_days = None
    if age_days is not None and age_days > _AUDIT_STALE_DAYS:
        status = "stale"
    elif fails >= 2:
        status = "fail"
    elif fails == 1 or errors:
        status = "warn"
    else:
        status = "ok"
    return {
        "status": status,
        "date": date_str,
        "age_days": age_days,
        "pass_count": passes,
        "fail_count": fails,
        "error_count": errors,
        "sampled": len(doc.get("samples") or []),
        "detail": None,
    }
