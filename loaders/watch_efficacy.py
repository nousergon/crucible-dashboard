"""
watch_efficacy.py — aggregate efficacy metrics for Watch Status (config#2389).

The Watch Status page (``views/37_Watch_Status.py``, formerly "Saturday SF
Watch") only ever showed a per-date drill-down: pick one date, see that
date's events. This module answers the operator's first question instead —
"how well is the watch actually doing, across every date on record" — by
reading EVERY Saturday SF Watch / Fleet CI Watch date file and rolling them
up into fix/escalation rates, MTTR, top failure modes and canary drill
health.

Design constraints (mirrors the per-date loaders in ``loaders/s3_loader.py``):
  - Reads via the shared ``get_s3_client()`` — no new IAM permissions.
  - Per-date fault tolerance: a date file that's missing, unreadable, or
    fails to parse is logged and SKIPPED rather than aborting the whole
    aggregation (one bad date must not blank the entire efficacy section).
  - Cached with ``st.cache_data(ttl=120)`` — watch-logs are append-only per
    date, so a short TTL keeps the aggregate reasonably fresh without
    re-reading every date file on every rerun.
  - Zero dates (the common case — most Saturdays / most days have no
    failures) produces zero-valued metrics, never a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import streamlit as st

from loaders.s3_loader import (
    list_ci_watch_dates,
    list_saturday_sf_watch_dates,
    load_ci_watch,
    load_latest_ci_watch_canary,
    load_latest_sf_watch_canary,
    load_saturday_sf_watch,
)

logger = logging.getLogger(__name__)

# Full autonomy shipped 2026-07-07 (all four dispatch flags true) — events
# from before this date may still show the pre-autonomy "observe" action and
# skew a plain fix-rate low; post_autonomy_fix_rate filters to this cutoff so
# the headline number reflects current-mode behavior. Mirrors the same
# cutoff referenced throughout views/37_Watch_Status.py.
_FULL_AUTONOMY_CUTOFF = "2026-07-07"

# Watcher actions that count as a "fix" vs an "escalation" for the rate
# metrics below. Mirrors the _ACTION_LABEL taxonomy in views/37_Watch_Status.py.
_FIX_ACTIONS = frozenset({"auto_fixed", "merged", "fixed_merged_rerun"})
_ESCALATION_ACTIONS = frozenset({"refused", "escalated"})
_OBSERVE_ACTIONS = frozenset({"observe"})

# Canary drills (config#2223) are not expected to have reported yet before
# this date — the weekly synthetic-drill cadence starts here.
CANARY_EXPECTED_FROM = "2026-07-23"

_TOP_FAILURE_MODES_LIMIT = 5


@dataclass(frozen=True)
class SfWatchEfficacy:
    """Aggregate metrics across all Saturday SF Watch dates."""

    total_dates: int = 0
    total_events: int = 0
    fix_rate: float = 0.0
    escalation_rate: float = 0.0
    observe_rate: float = 0.0
    post_autonomy_fix_rate: float | None = None
    mttr_hours: float | None = None
    top_failure_modes: list[tuple[str, int]] = field(default_factory=list)
    events_per_date: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class CiWatchEfficacy:
    """Aggregate metrics across all Fleet CI Watch dates."""

    total_dates: int = 0
    total_events: int = 0
    fix_rate: float = 0.0
    escalation_rate: float = 0.0
    per_repo: dict[str, int] = field(default_factory=dict)
    rerun_success_rate: float = 0.0
    events_per_date: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class CanaryEfficacy:
    """Weekly synthetic-drill health (config#2223) for both watches."""

    sf_watch_last_heartbeat: str | None = None
    sf_watch_age_days: float | None = None
    ci_watch_last_heartbeat: str | None = None
    ci_watch_age_days: float | None = None
    total_expected_drills: int = 0
    successful_drills: int = 0
    reliability: float = 0.0


@dataclass(frozen=True)
class WatchEfficacySnapshot:
    """Top-level aggregate efficacy snapshot rendered on the Watch Status page."""

    sf_watch: SfWatchEfficacy = field(default_factory=SfWatchEfficacy)
    ci_watch: CiWatchEfficacy = field(default_factory=CiWatchEfficacy)
    canary: CanaryEfficacy = field(default_factory=CanaryEfficacy)
    computed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def _parse_iso(raw: object) -> datetime | None:
    """Best-effort ISO-8601 parse (mirrors fleet_status_loader._parse_iso).
    Returns None on any unparseable / non-string value rather than raising —
    a single malformed timestamp inside an otherwise-valid event must not
    blow up the whole aggregation."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_dates_fault_tolerant(
    dates: list[str], loader, label: str
) -> list[tuple[str, dict]]:
    """Load every date in *dates* via *loader*, skipping (and logging) any
    date that comes back None (missing / unreadable / parse-error — the
    loader itself already collapses all three to None) or isn't a dict with
    a list ``events``. Returns ``[(date, doc), ...]`` for the dates that
    loaded cleanly, in the same order as *dates*."""
    out: list[tuple[str, dict]] = []
    for d in dates:
        try:
            doc = loader(d)
        except Exception as e:  # noqa: BLE001 — a per-date read must not
            # abort the whole aggregation; log + skip like every other
            # fault-tolerant loader in this module.
            logger.warning("%s: unexpected error loading %s: %s", label, d, e)
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
            logger.warning(
                "%s: %s unreadable or malformed — skipped from aggregation",
                label, d,
            )
            continue
        out.append((d, doc))
    return out


def _action_of(event: dict) -> str:
    return event.get("action") or "observe"


def _compute_sf_watch_efficacy(dated_docs: list[tuple[str, dict]]) -> SfWatchEfficacy:
    if not dated_docs:
        return SfWatchEfficacy()

    all_events: list[tuple[str, dict]] = [
        (d, e) for d, doc in dated_docs for e in doc["events"] if isinstance(e, dict)
    ]
    total_events = len(all_events)
    if total_events == 0:
        return SfWatchEfficacy(
            total_dates=len(dated_docs),
            events_per_date=[(d, 0, 0) for d, _ in dated_docs],
        )

    n_fix = sum(1 for _, e in all_events if _action_of(e) in _FIX_ACTIONS)
    n_escalated = sum(1 for _, e in all_events if _action_of(e) in _ESCALATION_ACTIONS)
    n_observe = sum(1 for _, e in all_events if _action_of(e) in _OBSERVE_ACTIONS)

    post_autonomy = [
        (d, e) for d, e in all_events if d >= _FULL_AUTONOMY_CUTOFF
    ]
    post_autonomy_fix_rate = (
        sum(1 for _, e in post_autonomy if _action_of(e) in _FIX_ACTIONS)
        / len(post_autonomy)
        if post_autonomy else None
    )

    failure_mode_counts: dict[str, int] = {}
    for _, e in all_events:
        mode = e.get("failed_state")
        if mode:
            failure_mode_counts[mode] = failure_mode_counts.get(mode, 0) + 1
    top_failure_modes = sorted(
        failure_mode_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:_TOP_FAILURE_MODES_LIMIT]

    events_per_date = [
        (
            d,
            len(doc["events"]),
            sum(
                1 for e in doc["events"]
                if isinstance(e, dict) and _action_of(e) in _FIX_ACTIONS
            ),
        )
        for d, doc in dated_docs
    ]

    mttr_hours = _mean_within_date_mttr_hours(dated_docs)

    return SfWatchEfficacy(
        total_dates=len(dated_docs),
        total_events=total_events,
        fix_rate=n_fix / total_events,
        escalation_rate=n_escalated / total_events,
        observe_rate=n_observe / total_events,
        post_autonomy_fix_rate=post_autonomy_fix_rate,
        mttr_hours=mttr_hours,
        top_failure_modes=top_failure_modes,
        events_per_date=events_per_date,
    )


def _mean_within_date_mttr_hours(
    dated_docs: list[tuple[str, dict]],
) -> float | None:
    """Mean, across dates that have both a first event and a fix event, of
    the within-date hours from the first event's ``detected_at`` to the
    first fix event's ``detected_at``. Cross-date MTTR is a non-goal for M1
    (a fix that lands on a later Saturday than the failure is out of scope
    here — future refinement per the config#2389 issue text)."""
    deltas: list[float] = []
    for _, doc in dated_docs:
        events = [e for e in doc["events"] if isinstance(e, dict)]
        timestamped = [
            (t, e) for e in events
            if (t := _parse_iso(e.get("detected_at"))) is not None
        ]
        if not timestamped:
            continue
        timestamped.sort(key=lambda te: te[0])
        first_ts = timestamped[0][0]
        fix_ts = next(
            (t for t, e in timestamped if _action_of(e) in _FIX_ACTIONS), None
        )
        if fix_ts is None:
            continue
        delta_hours = (fix_ts - first_ts).total_seconds() / 3600
        if delta_hours >= 0:
            deltas.append(delta_hours)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def _compute_ci_watch_efficacy(dated_docs: list[tuple[str, dict]]) -> CiWatchEfficacy:
    if not dated_docs:
        return CiWatchEfficacy()

    all_events: list[dict] = [
        e for _, doc in dated_docs for e in doc["events"] if isinstance(e, dict)
    ]
    total_events = len(all_events)
    if total_events == 0:
        return CiWatchEfficacy(
            total_dates=len(dated_docs),
            events_per_date=[(d, 0, 0) for d, _ in dated_docs],
        )

    n_fix = sum(1 for e in all_events if _action_of(e) in _FIX_ACTIONS)
    n_escalated = sum(1 for e in all_events if _action_of(e) in _ESCALATION_ACTIONS)

    per_repo: dict[str, int] = {}
    for e in all_events:
        repo = e.get("repo")
        if repo:
            per_repo[repo] = per_repo.get(repo, 0) + 1

    rerun_events = [e for e in all_events if e.get("rerun_conclusion")]
    rerun_success = sum(
        1 for e in rerun_events if e.get("rerun_conclusion") == "success"
    )
    rerun_success_rate = (rerun_success / len(rerun_events)) if rerun_events else 0.0

    events_per_date = [
        (
            d,
            len(doc["events"]),
            sum(
                1 for e in doc["events"]
                if isinstance(e, dict) and _action_of(e) in _FIX_ACTIONS
            ),
        )
        for d, doc in dated_docs
    ]

    return CiWatchEfficacy(
        total_dates=len(dated_docs),
        total_events=total_events,
        fix_rate=n_fix / total_events,
        escalation_rate=n_escalated / total_events,
        per_repo=per_repo,
        rerun_success_rate=rerun_success_rate,
        events_per_date=events_per_date,
    )


def _compute_canary_efficacy(now: datetime) -> CanaryEfficacy:
    """Canary drill health (config#2223) — reuses the existing
    ``load_latest_sf_watch_canary`` / ``load_latest_ci_watch_canary``
    loaders (same S3 listing + fault-tolerance as every other loader in
    ``loaders/s3_loader.py``), so no new S3 listing logic is needed here."""
    try:
        sf_hb = load_latest_sf_watch_canary()
    except Exception as e:  # noqa: BLE001
        logger.warning("watch_efficacy: sf_watch canary read failed: %s", e)
        sf_hb = None
    try:
        ci_hb = load_latest_ci_watch_canary()
    except Exception as e:  # noqa: BLE001
        logger.warning("watch_efficacy: ci_watch canary read failed: %s", e)
        ci_hb = None

    sf_age_days = _canary_age_days(now, sf_hb)
    ci_age_days = _canary_age_days(now, ci_hb)

    # Reliability is a simple present/absent tally across the two watches'
    # latest-known heartbeats — a full drill-history reliability trend is a
    # future refinement; M1 just answers "is each watch's dispatch pipe
    # currently proven alive".
    expected = now.date().isoformat() >= CANARY_EXPECTED_FROM
    total_expected = 2 if expected else 0
    successful = sum(1 for hb in (sf_hb, ci_hb) if hb) if expected else 0

    return CanaryEfficacy(
        sf_watch_last_heartbeat=(sf_hb or {}).get("date") if sf_hb else None,
        sf_watch_age_days=sf_age_days,
        ci_watch_last_heartbeat=(ci_hb or {}).get("date") if ci_hb else None,
        ci_watch_age_days=ci_age_days,
        total_expected_drills=total_expected,
        successful_drills=successful,
        reliability=(successful / total_expected) if total_expected else 0.0,
    )


def _canary_age_days(now: datetime, heartbeat: dict | None) -> float | None:
    """Days since the newest canary drill heartbeat, or None when no drill
    has ever reported. Mirrors fleet_status_loader._canary_age_hrs (hours),
    just in day units for the Watch Efficacy tile."""
    if not heartbeat:
        return None
    when = _parse_iso(heartbeat.get("drill_at"))
    if when is None:
        when = _parse_iso(f"{heartbeat.get('date')}T00:00:00+00:00")
    if when is None:
        return None
    return max(0.0, (now - when).total_seconds() / 86400)


@st.cache_data(ttl=120)
def load_watch_efficacy_snapshot() -> WatchEfficacySnapshot:
    """Aggregate efficacy metrics across ALL Saturday SF Watch + Fleet CI
    Watch dates (config#2389). Zero dates (no failures ever recorded, the
    common case) returns zero-valued metrics — never raises. Per-date read
    failures are logged and that date is excluded from the aggregate rather
    than aborting the whole snapshot.

    Cached 120s: the watch-logs are append-only per date, so a short TTL is
    enough to avoid re-reading every date file on each Streamlit rerun while
    staying reasonably fresh after a new dispatch.
    """
    now = datetime.now(timezone.utc)

    try:
        sf_dates = list_saturday_sf_watch_dates()
    except Exception as e:  # noqa: BLE001
        logger.warning("watch_efficacy: failed to list sf_watch dates: %s", e)
        sf_dates = []
    try:
        ci_dates = list_ci_watch_dates()
    except Exception as e:  # noqa: BLE001
        logger.warning("watch_efficacy: failed to list ci_watch dates: %s", e)
        ci_dates = []

    sf_dated_docs = _load_dates_fault_tolerant(
        sf_dates, load_saturday_sf_watch, "sf_watch"
    )
    ci_dated_docs = _load_dates_fault_tolerant(
        ci_dates, load_ci_watch, "ci_watch"
    )

    return WatchEfficacySnapshot(
        sf_watch=_compute_sf_watch_efficacy(sf_dated_docs),
        ci_watch=_compute_ci_watch_efficacy(ci_dated_docs),
        canary=_compute_canary_efficacy(now),
        computed_at=now,
    )
