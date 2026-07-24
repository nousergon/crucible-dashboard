"""
sla_status.py — pure SLA-resolution logic for the Fleet SLA / process-
completion console page (config#2858).

Answers "did each scheduled process complete within its SLA, and what's
its track record?" by CONSUMING the freshness-monitor's three existing
S3 planes — never re-probing S3 or re-deriving artifact presence itself
(config-I2861 reconciliation: this table renders the fleet's existing
monitoring planes, it does not become a fourth one):

  1. ``ARTIFACT_REGISTRY.yaml``  — the process/SLA definitions (cadence,
     sla_minutes_after_cron, owner_repo, severity) — the SoT.
  2. ``check_results.json``      — the freshness-monitor's own honest
     per-artifact judgment (``state``) for the CURRENT cycle. This
     module trusts that judgment; it does not recompute freshness.
  3. ``history.json``            — the daily historical-cycle probe
     (gap_count / lookback_cycles) — the source for the rolling hit-rate.

The one thing this module DOES compute independently is the display-layer
question "when is/was this cycle's SLA deadline" (``last_expected_utc``),
derived from the same fixed-UTC cron anchors ``fleet_status.py`` already
uses for the 3 top-level pipelines (imported, not duplicated) plus the
registry's own ``sla_minutes_after_cron`` — a presentation labeling, not
a freshness re-probe.

PURE: no streamlit, no boto3, no clock reads — the loader
(``loaders/sla_status_loader.py``) gathers an :class:`SlaInputs` snapshot
and everything here is a deterministic function of it, so the full
MET/BREACHED/PENDING/NOT_EXPECTED matrix is unit-testable with a frozen
``now`` (``tests/test_sla_status.py``), mirroring ``fleet_status.py``'s
own pure-resolver + frozen-clock pattern exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, timezone
from typing import Optional

from fleet_status import PREOPEN_CRON_UTC, WEEKLY_CRON_UTC, market_hours_utc

# Verdicts (config#2858 acceptance criteria vocabulary).
MET = "MET"
BREACHED = "BREACHED"
PENDING = "PENDING"
NOT_EXPECTED = "NOT_EXPECTED"

_VERDICT_SEVERITY = {BREACHED: 3, PENDING: 2, NOT_EXPECTED: 1, MET: 0}

# How far back a cadence-window search walks looking for the most recent
# past cron firing before giving up (holiday clusters + long weekends are
# well under this).
_MAX_WINDOW_LOOKBACK_DAYS = 10

_CADENCE_TRIGGER = {
    "saturday_sf": "Sat 09:00 UTC (weekly)",
    "weekday_sf": "weekdays, 12:45 UTC (pre-open)",
    "eod_sf": "weekdays, ~market close + 2h (post-close)",
    "continuous": "continuous",
}

_CADENCE_PIPELINE = {
    "saturday_sf": "Weekly pipeline (ne-weekly-freshness)",
    "weekday_sf": "Pre-open pipeline (ne-preopen-trading)",
    "eod_sf": "Post-close pipeline (ne-postclose-trading)",
    "continuous": "Continuous",
}

# The freshness-monitor's own honest per-cycle judgment (config-I2861: the
# authority for "is this artifact fresh right now" — never recomputed
# here). ``grace_period`` is a still-cold-starting row (created_at +
# grace_period_cycles hasn't elapsed) — PENDING, not yet expected to have
# completed even once. ``fresh`` covers both "just completed on time" and
# "previous cycle still within its SLA window" — MET either way, since the
# freshness substrate's own window math already folded the SLA deadline in.
_STATE_VERDICT = {
    "fresh": MET,
    "grace_period": PENDING,
    "stale": BREACHED,
    "missing": BREACHED,
    "probe_failed": BREACHED,
}


@dataclass(frozen=True)
class SlaRegistryRow:
    """One ``ARTIFACT_REGISTRY.yaml`` entry, condensed to what the
    resolver needs (mirrors the Lambda's ``ArtifactSpec`` fields the
    table cares about)."""

    artifact_id: str
    cadence: str  # saturday_sf | weekday_sf | eod_sf | continuous
    sla_minutes_after_cron: int
    owner_repo: str
    severity: str


@dataclass(frozen=True)
class SlaProcessRow:
    process_id: str
    display_name: str
    pipeline: str
    trigger: str
    cadence: str
    sla_minutes_after_cron: Optional[int]
    last_expected_utc: Optional[datetime]
    last_completed_utc: Optional[datetime]
    verdict: str  # MET | BREACHED | PENDING | NOT_EXPECTED
    hit_rate_30d: Optional[float]  # None ⇒ no history coverage for this row
    lookback_cycles: Optional[int] = None
    owner_repo: Optional[str] = None
    severity: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class SlaInputs:
    """Everything the resolver needs, gathered by the loader in one pass."""

    now: datetime  # tz-aware UTC
    registry: tuple = ()  # tuple[SlaRegistryRow, ...]
    check_results: Optional[dict] = None  # raw check_results.json
    history: Optional[dict] = None  # raw history.json


# ── Cadence window math (reuses fleet_status.py's fixed-UTC anchors) ───────


def _utc_at(d: date, t) -> datetime:
    return datetime.combine(d, t, tzinfo=timezone.utc)


def _is_trading_day(d: date) -> bool:
    from trading_calendar import is_trading_day

    return is_trading_day(d)


def most_recent_cron_firing(cadence: str, now: datetime) -> Optional[datetime]:
    """Most recent past firing time (<= now) of the cadence's SF cron, or
    None for ``continuous`` (no discrete firing) or if none is found
    within the lookback window (registry created before any firing).

    Single backward day-walk from ``now``'s date, evaluating each
    calendar day against the cadence's own weekday/trading-day rule and
    the fixed-UTC cron anchor (imported from ``fleet_status.py`` — same
    anchors the top-level pipeline dots use, never re-declared).
    ``eod_sf``'s anchor is the trading day's market close (computed at
    UTC noon on that day so the ET-date conversion never crosses
    midnight, regardless of DST) — the registry's own
    ``sla_minutes_after_cron`` is defined relative to that close, mirroring
    the freshness-monitor substrate's own cadence semantics.
    """
    if cadence == "saturday_sf":
        d = now.date()
        for _ in range(7 + _MAX_WINDOW_LOOKBACK_DAYS):
            if d.weekday() == 5:
                candidate = _utc_at(d, WEEKLY_CRON_UTC)
                if candidate <= now:
                    return candidate
            d -= timedelta(days=1)
        return None
    if cadence == "weekday_sf":
        d = now.date()
        for _ in range(_MAX_WINDOW_LOOKBACK_DAYS):
            if _is_trading_day(d):
                candidate = _utc_at(d, PREOPEN_CRON_UTC)
                if candidate <= now:
                    return candidate
            d -= timedelta(days=1)
        return None
    if cadence == "eod_sf":
        d = now.date()
        for _ in range(_MAX_WINDOW_LOOKBACK_DAYS):
            if _is_trading_day(d):
                _, close_utc = market_hours_utc(_utc_at(d, dtime(12, 0)))
                if close_utc <= now:
                    return close_utc
            d -= timedelta(days=1)
        return None
    return None  # continuous


# ── Hit-rate (from history.json) ────────────────────────────────────────────


def _hit_rate(history_entry: Optional[dict]) -> tuple[Optional[float], Optional[int]]:
    """(hit_rate, lookback_cycles) from one history.json artifact entry.
    None/None when there's no usable rolling-window coverage (absent
    entry, a latest-pointer artifact with no cycle sequence, or a
    zero-length lookback)."""
    if not history_entry or history_entry.get("is_latest_pointer"):
        return None, None
    gap = history_entry.get("gap_count")
    lookback = history_entry.get("lookback_cycles")
    if gap is None or not lookback:
        return None, lookback
    return round((lookback - gap) / lookback, 4), lookback


# ── Per-process resolver ─────────────────────────────────────────────────────


def _parse_iso_utc(raw) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_process(
    reg: SlaRegistryRow,
    check_row: Optional[dict],
    history_entry: Optional[dict],
    now: datetime,
) -> SlaProcessRow:
    trigger = _CADENCE_TRIGGER.get(reg.cadence, reg.cadence)
    pipeline = _CADENCE_PIPELINE.get(reg.cadence, reg.cadence)
    hit_rate, lookback = _hit_rate(history_entry)

    if reg.cadence == "continuous":
        # No discrete cron deadline — verdict is a direct read of the
        # monitor's own current state, no window math to do.
        if check_row is None:
            verdict, last_completed, reason = NOT_EXPECTED, None, "no check_results row"
        else:
            verdict = _STATE_VERDICT.get(check_row.get("state"), NOT_EXPECTED)
            last_completed = _parse_iso_utc(check_row.get("last_modified"))
            reason = check_row.get("reason")
        return SlaProcessRow(
            reg.artifact_id, reg.artifact_id, pipeline, trigger, reg.cadence,
            reg.sla_minutes_after_cron, None, last_completed, verdict, hit_rate,
            lookback, reg.owner_repo, reg.severity, reason,
        )

    firing = most_recent_cron_firing(reg.cadence, now)
    due = (
        firing + timedelta(minutes=reg.sla_minutes_after_cron)
        if firing is not None
        else None
    )
    last_completed = _parse_iso_utc(check_row.get("last_modified")) if check_row else None
    reason = check_row.get("reason") if check_row else None

    if firing is None or due is None:
        # No cadence firing found in the lookback window (e.g. a brand-new
        # registry row before its first-ever cron) — nothing to judge yet.
        verdict = NOT_EXPECTED
    elif check_row is None:
        # Registry carries this row but no check_results probe has covered
        # it yet — time-only fallback (never invents a MET without a
        # monitor-confirmed landing).
        verdict = PENDING if now < due else BREACHED
    else:
        # Trust the freshness monitor's own honest per-cycle judgment
        # (config-I2861: this table renders the monitor, it never
        # re-derives freshness) — last_expected_utc above is display
        # context only, not an input to this verdict.
        verdict = _STATE_VERDICT.get(check_row.get("state"), NOT_EXPECTED)

    return SlaProcessRow(
        reg.artifact_id, reg.artifact_id, pipeline, trigger, reg.cadence,
        reg.sla_minutes_after_cron, due, last_completed, verdict, hit_rate,
        lookback, reg.owner_repo, reg.severity, reason,
    )


def resolve_sla_table(inp: SlaInputs) -> list[SlaProcessRow]:
    """All registry rows resolved to :class:`SlaProcessRow`, in registry
    order (the page groups/sorts for display)."""
    check_by_id = {
        r.get("artifact_id"): r
        for r in (inp.check_results or {}).get("results", []) or []
        if isinstance(r, dict)
    }
    history_by_id = (inp.history or {}).get("artifacts", {}) or {}
    return [
        resolve_process(
            reg, check_by_id.get(reg.artifact_id), history_by_id.get(reg.artifact_id),
            inp.now,
        )
        for reg in inp.registry
    ]


def worst_verdict(rows: list[SlaProcessRow]) -> Optional[str]:
    """Severity rollup (BREACHED > PENDING > NOT_EXPECTED > MET), None for
    an empty table."""
    if not rows:
        return None
    return max((r.verdict for r in rows), key=lambda v: _VERDICT_SEVERITY.get(v, 0))
