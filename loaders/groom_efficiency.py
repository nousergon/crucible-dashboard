"""Pure helpers: match groom run artifacts to usage files + efficiency ratios.

Run artifacts live at ``groom/{date}/{run_id}.json``; groom usage at
``claude_code_usage/groom/{date}/{usage_run_id}.json``. On EC2 spot the IDs
differ (``GROOM_RUN_TOKEN`` vs ``{timestamp}-{instance_id}``), so we join by
date + nearest timestamp to estimated run end.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

_USAGE_KEY_RE = re.compile(
    r"^claude_code_usage/groom/(\d{4}-\d{2}-\d{2})/([^/]+)\.json$"
)
_USAGE_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)")
_MANUAL_RESET_PREFIX = "zz-manual"

# Tier-aware alert thresholds (from observed 2026-07 runs; tune at recalibration).
_WET_PER_ENGAGED_CEILING: dict[str, float] = {
    "low-only": 80_000,
    "mid-only": 500_000,
    "high-only": 700_000,
    "default": 500_000,
}
_THROUGHPUT_FLOOR_ISSUES_PER_MIN: dict[str, float] = {
    "low-only": 0.4,
    "mid-only": 0.15,
    "high-only": 0.12,
    "default": 0.15,
}
_UNTOUCHED_WARN_FRAC = 0.10


def parse_run_start(run: dict[str, Any]) -> datetime | None:
    raw = run.get("run_start")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def estimate_run_end(run: dict[str, Any]) -> datetime | None:
    start = parse_run_start(run)
    if start is None:
        return None
    elapsed = run.get("elapsed_min")
    if elapsed is None:
        return start
    try:
        return start + timedelta(minutes=int(elapsed))
    except (TypeError, ValueError):
        return start


def parse_usage_key_timestamp(key: str) -> datetime | None:
    m = _USAGE_KEY_RE.match(key)
    if not m:
        return None
    suffix = m.group(2)
    if suffix.startswith(_MANUAL_RESET_PREFIX):
        return None
    ts_m = _USAGE_TS_RE.match(suffix)
    if not ts_m:
        return None
    try:
        return datetime.strptime(ts_m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def usage_record_from_doc(key: str, doc: dict[str, Any]) -> dict[str, Any] | None:
    m = _USAGE_KEY_RE.match(key)
    if not m:
        return None
    if m.group(2).startswith(_MANUAL_RESET_PREFIX):
        return None
    day = doc.get("day_total") or {}
    total = int(day.get("total") or 0)
    cache_read = int(day.get("cache_read_input_tokens") or 0)
    return {
        "key": key,
        "wet": float(day.get("wet") or 0),
        "total": total,
        "cache_read": cache_read,
        "cache_read_pct": (100.0 * cache_read / total) if total else None,
        "ts": parse_usage_key_timestamp(key),
    }


def match_usage_for_run(
    run_key: str,
    run: dict[str, Any],
    usage_records: list[dict[str, Any]],
    *,
    assigned: set[str] | None = None,
    max_delta_minutes: int = 45,
) -> dict[str, Any] | None:
    """Return the best usage record for this run, or None."""
    if assigned is None:
        assigned = set()
    m = re.match(r"^groom/(\d{4}-\d{2}-\d{2})/([^/]+)\.json$", run_key)
    if not m:
        return None
    date, run_id = m.group(1), m.group(2)
    direct = f"claude_code_usage/groom/{date}/{run_id}.json"
    by_key = {u["key"]: u for u in usage_records}
    if direct in by_key and direct not in assigned:
        return by_key[direct]

    end = estimate_run_end(run)
    if end is None:
        return None
    max_delta = timedelta(minutes=max_delta_minutes)
    best: dict[str, Any] | None = None
    best_delta = max_delta
    for rec in usage_records:
        if rec["key"] in assigned:
            continue
        if not rec["key"].startswith(f"claude_code_usage/groom/{date}/"):
            continue
        ts = rec.get("ts")
        if ts is None:
            continue
        delta = abs(ts - end)
        if delta <= best_delta:
            best_delta = delta
            best = rec
    return best


def disposition_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    keys = ("closed", "pr_opened", "commented", "untouched")
    return {d: sum(1 for i in issues if i.get("disposition") == d) for d in keys}


def short_model_name(model: str | None) -> str:
    """``claude-opus-4-8`` -> ``opus-4-8`` — drop the vendor prefix so the Run
    history table and Model scorecard (config-I2746) stay narrow."""
    if not model:
        return "—"
    return model.removeprefix("claude-") if model.startswith("claude-") else model


def compute_efficiency(
    run: dict[str, Any],
    issues: list[dict[str, Any]],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Outcome + token efficiency metrics for one run."""
    counts = disposition_counts(issues)
    engaged = int(run.get("engaged") or 0)
    if engaged == 0:
        engaged = sum(counts[d] for d in ("closed", "pr_opened", "commented"))
    total = int(run.get("total_issues") or len(issues) or 0)
    elapsed = int(run.get("elapsed_min") or 0)
    soft = int(run.get("soft_limit_min") or 0)
    tier = str(run.get("issue_filter") or "default")
    hard = counts["closed"] + counts["pr_opened"]

    wet = float(usage["wet"]) if usage else None
    cache_pct = usage.get("cache_read_pct") if usage else None
    # config#1894: schema_version >= 5 artifacts carry the run's OWN measured
    # WET (driver-computed from its local transcripts at artifact-write time) —
    # exact per-run attribution, so it takes precedence over the date +
    # nearest-end-time usage-record join above (which stays as the fallback for
    # pre-schema-5 runs and as the source of cache_read_pct either way).
    artifact_wet = run.get("run_wet")
    if artifact_wet is not None:
        wet = float(artifact_wet)

    wet_per_engaged = (wet / engaged) if (wet is not None and engaged > 0) else None
    wet_per_hard = (wet / hard) if (wet is not None and hard > 0) else None
    throughput = (engaged / elapsed) if elapsed > 0 else None
    untouched_frac = (counts["untouched"] / total) if total > 0 else None
    hard_rate = (hard / engaged) if engaged > 0 else None
    comment_rate = (counts["commented"] / engaged) if engaged > 0 else None
    budget_pct = (100.0 * elapsed / soft) if soft > 0 else None
    disposition_rate = (engaged / total) if total > 0 else None

    alerts: list[str] = []
    if run.get("floor_fail"):
        alerts.append("floor breach")
    if untouched_frac is not None and untouched_frac > _UNTOUCHED_WARN_FRAC:
        alerts.append(f"high untouched ({counts['untouched']}/{total})")
    wet_ceil = _WET_PER_ENGAGED_CEILING.get(tier, _WET_PER_ENGAGED_CEILING["default"])
    if wet_per_engaged is not None and wet_per_engaged > wet_ceil:
        alerts.append(f"high WET/issue ({wet_per_engaged/1e3:.0f}K)")
    thr_floor = _THROUGHPUT_FLOOR_ISSUES_PER_MIN.get(
        tier, _THROUGHPUT_FLOOR_ISSUES_PER_MIN["default"]
    )
    if throughput is not None and engaged >= 8 and throughput < thr_floor:
        alerts.append(f"slow throughput ({throughput:.2f}/min)")

    return {
        "wet": wet,
        "wet_per_engaged": wet_per_engaged,
        "wet_per_hard": wet_per_hard,
        "throughput": throughput,
        "cache_read_pct": cache_pct,
        "hard_rate": hard_rate,
        "comment_rate": comment_rate,
        "untouched_frac": untouched_frac,
        "budget_pct": budget_pct,
        "disposition_rate": disposition_rate,
        "engaged": engaged,
        "usage_matched": usage is not None,
        "usage_key": usage["key"] if usage else None,
        "alerts": alerts,
    }


def model_scorecard_rows(
    runs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Aggregate ``(run, eff)`` pairs — the same tuples already built for Run
    history, ``eff`` from ``compute_efficiency`` — into one row per
    ``(model, issue_filter)`` group (config-I2746).

    Degenerate wind-downs (``eff["engaged"] == 0``) are excluded entirely —
    they're not real coverage attempts. Mixed-filter runs (``high+mid+low``)
    are grouped by the literal ``issue_filter`` string rather than attempting
    per-tier attribution (can't be split from per-issue records). Runs with
    no measured WET (pre-2026-07-07 schema, or an unmatched usage join) don't
    contribute to the WET aggregates — group totals render ``"—"`` rather
    than crash or silently understate cost per hard outcome.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for run, eff in runs:
        engaged = eff.get("engaged") or 0
        if engaged == 0:
            continue
        model = short_model_name(run.get("model"))
        tier = run.get("issue_filter") or "—"
        g = groups.setdefault((model, tier), {
            "runs": 0, "touches": 0, "hard": 0, "commented": 0,
            "untouched": 0, "queued": 0, "wet_sum": 0.0, "wet_hard": 0,
            "wet_runs": 0, "crashes": 0, "elapsed": 0, "processed": 0,
        })
        issues = run.get("issues") or []
        counts = disposition_counts(issues)
        hard = counts["closed"] + counts["pr_opened"]
        g["runs"] += 1
        g["touches"] += engaged
        g["hard"] += hard
        g["commented"] += counts["commented"]
        g["untouched"] += counts["untouched"]
        g["queued"] += int(run.get("total_issues") or len(issues) or 0)
        wet = eff.get("wet")
        if wet is not None:
            g["wet_sum"] += wet
            g["wet_hard"] += hard
            g["wet_runs"] += 1
        if "crash" in str(run.get("stop_reason") or "").lower():
            g["crashes"] += 1
        g["elapsed"] += int(run.get("elapsed_min") or 0)
        g["processed"] += int(run.get("processed") or len(issues) or 0)

    rows: list[dict[str, Any]] = []
    for (model, tier), g in groups.items():
        touches = g["touches"]
        rows.append({
            "Model": model,
            "Tier": tier,
            "Runs": g["runs"],
            "Touches": touches,
            "Hard-outcome rate": f"{100 * g['hard'] / touches:.0f}%" if touches else "—",
            "Comment-only %": f"{100 * g['commented'] / touches:.0f}%" if touches else "—",
            "Untouched %": f"{100 * g['untouched'] / g['queued']:.0f}%" if g["queued"] else "—",
            "Total WET": f"{g['wet_sum'] / 1e6:.1f}M" if g["wet_runs"] else "—",
            "WET/hard": f"{g['wet_sum'] / g['wet_hard'] / 1e3:.0f}K" if g["wet_hard"] else "—",
            "Crashes": g["crashes"],
            "Min/issue": f"{g['elapsed'] / g['processed']:.1f}" if g["processed"] else "—",
        })
    rows.sort(key=lambda r: (r["Model"], r["Tier"]))
    return rows
