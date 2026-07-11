"""
fleet_status.py — pure status-resolution logic for the Fleet Status console page.

Composes the fleet's existing status planes into per-component dots:

  🟢 green  — actively running right now (continuous service online, or a
              scheduled pipeline mid-execution)
  🟡 yellow — should be live/fresh right now but is stalled (grace-period,
              stale heartbeat, SSM ping lost, overdue scheduled start)
  🔴 red    — expected and offline / failed / missing past SLA
  ⚪ gray   — not currently active: off-hours/weekend/holiday, a scheduled
              pipeline idle between runs (last cycle complete, not due yet),
              or no signal available; shows last-known state

Scheduled pipelines are event-driven, not continuous services — idle is
their normal 99%-of-the-time state, so a completed-and-idle pipeline reads
⚪ (like an off-hours continuous service), not 🟢. 🟢 is reserved for an
actual live execution. Only a genuine SLA disruption (overdue start,
partial/failed cycle) escalates to 🟡/🔴 — never idle-after-success.

The status planes composed here (independent-freshness-as-authority,
config#1724 — self-reported health is enrichment, never the authority):

  1. AWS control plane      — EC2 instance state + SSM agent PingStatus
                              (the authority for "is the box up").
  2. Step Functions         — live execution projections via
                              ``nousergon_lib.pipeline_status`` (page 25's
                              substrate) + artifact-completion verdicts.
  3. Freshness monitor      — ``_freshness_monitor/heartbeat.json`` +
                              ``check_results.json`` (15-min independent
                              artifact probes against ARTIFACT_REGISTRY SLAs).
  4. Producer heartbeats    — daemon ``intraday/nav.json`` 60s writes,
                              groom run artifacts + in-progress marker.
  5. Module self-reports    — ``health/{module}.json`` (enrichment row).

This module is PURE: no streamlit, no boto3, no clock reads — the loader
(``loaders/fleet_status_loader.py``) gathers a :class:`FleetInputs` snapshot
and everything here is a deterministic function of it, so the full
green/yellow/red/gray matrix is unit-testable with a frozen ``now``
(``tests/test_fleet_status.py``).

Schedule expectations are anchored the same way the fleet's crons are:
fixed-UTC anchors for the EventBridge-cron-driven windows (pre-open SF at
12:45 UTC), exchange-local (America/New_York) for market-hours windows so
DST never skews the daemon expectation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Dot states (severity-ordered for rollups: red > yellow > gray > green).
GREEN = "green"
YELLOW = "yellow"
RED = "red"
GRAY = "gray"

_DOT_ICONS = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴", GRAY: "⚪"}
_DOT_SEVERITY = {RED: 3, YELLOW: 2, GRAY: 1, GREEN: 0}

# Component groups, in render order.
GROUP_INFRA = "Infrastructure (continuous)"
GROUP_PIPELINES = "Pipelines (scheduled)"
GROUP_JOBS = "Jobs & Agents"
GROUP_DATA = "Data & Artifacts"
GROUP_ORDER = (GROUP_INFRA, GROUP_PIPELINES, GROUP_JOBS, GROUP_DATA)

# ── Schedule anchors ────────────────────────────────────────────────────────
# Pre-open SF EventBridge cron: 12:45 UTC Mon–Fri (fixed UTC by design).
PREOPEN_CRON_UTC = dtime(12, 45)
# Weekly SF EventBridge cron: Sat 09:00 UTC.
WEEKLY_CRON_UTC = dtime(9, 0)
# Grace after a cron fires before "hasn't started" turns yellow.
CRON_START_GRACE = timedelta(minutes=15)
# Trading instance is expected up from the pre-open cron (+boot grace)
# until ~75 min after market close (EOD SF stops it).
INSTANCE_STOP_LAG = timedelta(minutes=75)
# Daemon heartbeat (intraday/nav.json is rewritten every ~60 s poll):
# stale past this within market hours ⇒ stalled.
DAEMON_STALE_S = 300.0
# Daemon expectation starts a little after the open (first poll settles).
DAEMON_OPEN_GRACE = timedelta(minutes=5)
# Post-close pipeline expected complete by close + this.
POSTCLOSE_DUE_LAG = timedelta(hours=2)
# Freshness-monitor Lambda runs every 15 min.
FRESHNESS_HEARTBEAT_STALE_S = 25 * 60.0
FRESHNESS_HEARTBEAT_DEAD_S = 60 * 60.0
# Groom cadence is 3×/day (2 Sonnet + 1 Opus) — recency tiers.
GROOM_MARKER_STALE = timedelta(hours=4)
GROOM_IDLE_OK = timedelta(hours=10)
GROOM_IDLE_WARN = timedelta(hours=30)
# Watch-dispatch canary drill (config#2223): a weekly synthetic drill
# (EventBridge Scheduler → the two spot-dispatcher Lambdas, Wed 15:00/15:30
# UTC) exercises the dispatch pipe end-to-end — Lambda IAM, scoped
# RunInstances, SSM, bootstrap start — and writes
# ``consolidated/{saturday_sf_watch,ci_watch}/_canary/{date}.json`` on
# success. Unlike the last-fired date (which cannot distinguish "healthy
# idle" from "silently broken idle"), a missed canary is a REAL "should have
# run and didn't" signal: one missed weekly run (>8 d) ⇒ YELLOW; two
# consecutive misses (>15 d) ⇒ RED; no heartbeat at all once the canary is
# due to have run ⇒ RED.
CANARY_STALE = timedelta(days=8)
CANARY_DEAD = timedelta(days=15)
# First moment a heartbeat MUST exist. The canary shipped with config#2223
# (2026-07-11) but its Scheduler rules are operator-applied and the drills
# fire Wednesdays — this anchor allows a full apply-plus-first-Wednesday
# window before "never reported" escalates to RED (before it: GRAY note, not
# a false alarm on a not-yet-applied schedule).
CANARY_EXPECTED_FROM_UTC = datetime(2026, 7, 23, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PipelineSnapshot:
    """Condensed projection of one Step Function's most recent run.

    ``status`` is the lib RunStatus name ("RUNNING"/"SUCCEEDED"/"FAILED"/
    "NOT_RUN") or a loader-level outcome ("NO_EXECUTIONS"/"UNAVAILABLE").
    ``verdict`` is the artifact-completion CycleVerdict ("COMPLETE"/
    "PARTIAL"/"FAILED"/"RUNNING"/"NOT_RUN") — the honest cycle judgment
    (config#727: a run that wrote every artifact but tripped a terminal
    Catch still reports SF FAILED).
    """

    status: str
    verdict: Optional[str] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    current_state: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class GroomSnapshot:
    """Groomer state: the in-progress marker (written at run start by
    ``groom_driver.py``, finalized at run end) + the newest run artifact.
    ``marker`` is None when absent OR when the driver predates the marker
    (consumers must tolerate absence — recency tiers then decide)."""

    marker_started_at: Optional[datetime] = None
    marker_tier: Optional[str] = None
    marker_model: Optional[str] = None
    last_run_start: Optional[datetime] = None
    last_stop_reason: Optional[str] = None
    last_model: Optional[str] = None
    # Independent control-plane signal: a running alpha-engine-groom-spot
    # EC2 instance. Covers runs the marker can't — a run launched on
    # pre-marker driver code, or a driver that died before finalizing.
    spot_running: bool = False
    spot_launched_at: Optional[datetime] = None


@dataclass(frozen=True)
class ModuleHealthRow:
    """One health/{module}.json self-report (enrichment plane).

    ``stale_after_hrs`` is the module's own expected cadence/SLA (from
    ``nousergon_lib.health.DASHBOARD_HEALTH_MODULES``) — the resolver
    checks ``age_hrs`` against it independently of the self-reported
    ``status``, per config#1724 (self-report is enrichment, never
    authority): a writer that died silently still has its last "ok" stamp
    sitting in S3 forever, so staleness must be caught even when the
    module never told us anything was wrong.
    """

    module: str
    status: str  # ok | degraded | failed | unknown
    age_hrs: Optional[float] = None
    error: Optional[str] = None
    stale_after_hrs: Optional[float] = None


@dataclass(frozen=True)
class FleetInputs:
    """Everything the resolver needs, gathered by the loader in one pass."""

    now: datetime  # tz-aware UTC
    is_trading_day: bool
    # AWS control plane (None fields ⇒ that probe was unavailable).
    ec2_available: bool = True
    ec2_error: Optional[str] = None
    trading_instance_state: Optional[str] = None  # running|stopped|...
    trading_instance_ping: Optional[str] = None  # Online|ConnectionLost|...
    # Local box services.
    live_service_ok: Optional[bool] = None  # None ⇒ probe n/a (off-box dev)
    # Daemon heartbeat: age (s) of intraday/nav.json; None ⇒ artifact absent.
    intraday_nav_age_s: Optional[float] = None
    # Pipelines keyed weekly|preopen|postclose.
    pipelines: dict = field(default_factory=dict)
    # Freshness monitor artifacts (raw JSON dicts).
    heartbeat: Optional[dict] = None
    check_results: Optional[dict] = None
    groom: GroomSnapshot = field(default_factory=GroomSnapshot)
    module_health: tuple = ()  # tuple[ModuleHealthRow, ...]
    # Fleet-SF Watch / Fleet CI Watch (config#1227/#1593) — failure-driven
    # dispatch agents, not continuous services: idle is the healthy steady
    # state (see resolve_sf_watch/resolve_ci_watch). last_date/n_events come
    # from the newest ``consolidated/{saturday_sf_watch,ci_watch}/{date}.json``
    # watch-log; alert is the title of an open P1 "dispatch failed to
    # launch" issue (sf-watch.yml's own failure reporting) — None when the
    # dispatch mechanism itself hasn't been caught broken.
    sf_watch_last_date: Optional[str] = None
    sf_watch_last_n_events: int = 0
    sf_watch_alert: Optional[str] = None
    ci_watch_last_date: Optional[str] = None
    ci_watch_last_n_events: int = 0
    ci_watch_alert: Optional[str] = None
    # Live repair box — the "is it working RIGHT NOW" signal. A running EC2
    # spot instance tagged as the watch's repair box, independent of any S3
    # watch-log artifact (same posture as GroomSnapshot.spot_running): covers
    # a charter mid-run before any event is written — on 2026-07-11 two
    # operator re-fires wrote NO canonical watch-log, so these dots showed
    # idle while a box was actively repairing the weekly SF.
    sf_watch_box_running: bool = False
    sf_watch_box_launched_at: Optional[str] = None
    ci_watch_box_running: bool = False
    ci_watch_box_launched_at: Optional[str] = None
    # Canary drill heartbeat age (config#2223) — hours since the newest
    # ``consolidated/{saturday_sf_watch,ci_watch}/_canary/{date}.json``
    # drill_at. None ⇒ no heartbeat artifact exists (yet): RED once
    # CANARY_EXPECTED_FROM_UTC has passed, benign before it.
    sf_watch_canary_age_hrs: Optional[float] = None
    ci_watch_canary_age_hrs: Optional[float] = None


@dataclass(frozen=True)
class ComponentStatus:
    component_id: str
    label: str
    group: str
    dot: str  # GREEN | YELLOW | RED | GRAY
    reason: str
    last_activity_utc: Optional[datetime] = None
    detail: tuple = ()  # tuple[dict, ...] — rows for the expander
    deep_link: Optional[str] = None  # console slug, e.g. "pipeline-status"

    @property
    def icon(self) -> str:
        return _DOT_ICONS.get(self.dot, "⚪")


# ── Time helpers (pure; all take/return tz-aware UTC) ───────────────────────


def _utc_today_at(now: datetime, t: dtime) -> datetime:
    return datetime.combine(now.date(), t, tzinfo=timezone.utc)


def market_hours_utc(now: datetime) -> tuple[datetime, datetime]:
    """Today's NYSE regular session (09:30–16:00 America/New_York) as UTC.

    Computed exchange-local so DST never skews it. Does NOT model early
    closes (half-days show a benign gray hour, not a false red).
    """
    local = now.astimezone(_ET).date()
    open_et = datetime.combine(local, dtime(9, 30), tzinfo=_ET)
    close_et = datetime.combine(local, dtime(16, 0), tzinfo=_ET)
    return open_et.astimezone(timezone.utc), close_et.astimezone(timezone.utc)


def trading_instance_window(now: datetime) -> tuple[datetime, datetime]:
    """Window in which the trading instance is expected UP on a trading day:
    pre-open cron + boot grace → market close + EOD stop lag."""
    _, close_utc = market_hours_utc(now)
    start = _utc_today_at(now, PREOPEN_CRON_UTC) + CRON_START_GRACE
    return start, close_utc + INSTANCE_STOP_LAG


def daemon_window(now: datetime) -> tuple[datetime, datetime]:
    """Window in which the daemon heartbeat is expected fresh."""
    open_utc, close_utc = market_hours_utc(now)
    return open_utc + DAEMON_OPEN_GRACE, close_utc + timedelta(minutes=5)


def _ago(now: datetime, then: Optional[datetime]) -> str:
    if then is None:
        return "never"
    s = max(0.0, (now - then).total_seconds())
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.1f} d ago"


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


# ── Per-component resolvers ─────────────────────────────────────────────────


def resolve_trading_instance(inp: FleetInputs) -> ComponentStatus:
    cid, label = "trading_instance", "Trading instance (EC2)"
    start, end = trading_instance_window(inp.now)
    expected = inp.is_trading_day and start <= inp.now <= end

    if not inp.ec2_available:
        return ComponentStatus(
            cid, label, GROUP_INFRA,
            YELLOW if expected else GRAY,
            f"EC2 status unavailable — {inp.ec2_error or 'unknown error'}",
        )

    state = inp.trading_instance_state or "unknown"
    ping = inp.trading_instance_ping
    if state == "running":
        if ping and ping != "Online":
            return ComponentStatus(
                cid, label, GROUP_INFRA, YELLOW,
                f"instance running but SSM agent {ping} — box may be wedged",
            )
        note = "" if expected else " (outside scheduled trading window)"
        ping_note = "SSM agent Online" if ping == "Online" else "SSM ping unknown"
        return ComponentStatus(
            cid, label, GROUP_INFRA, GREEN, f"online — {ping_note}{note}",
        )
    if expected:
        return ComponentStatus(
            cid, label, GROUP_INFRA, RED,
            f"instance {state} during the trading window "
            f"(expected up {start.strftime('%H:%M')}–{end.strftime('%H:%M')} UTC)",
        )
    return ComponentStatus(
        cid, label, GROUP_INFRA, GRAY,
        f"{state} — outside trading window"
        + ("" if inp.is_trading_day else " (market closed today)"),
    )


def resolve_daemon(inp: FleetInputs) -> ComponentStatus:
    cid, label = "trading_daemon", "Intraday daemon"
    start, end = daemon_window(inp.now)
    expected = inp.is_trading_day and start <= inp.now <= end
    age = inp.intraday_nav_age_s
    last = (
        inp.now - timedelta(seconds=age) if age is not None else None
    )

    if expected:
        if age is None:
            return ComponentStatus(
                cid, label, GROUP_INFRA, RED,
                "no intraday heartbeat artifact (intraday/nav.json) during market hours",
                deep_link="pipeline-status",
            )
        if age <= DAEMON_STALE_S:
            return ComponentStatus(
                cid, label, GROUP_INFRA, GREEN,
                f"heartbeat {_ago(inp.now, last)}", last,
            )
        # Stale within the session: heartbeat from THIS session ⇒ stalled
        # (yellow); a heartbeat that predates today's open ⇒ the daemon
        # never came up this session (red).
        if last is not None and last < start:
            return ComponentStatus(
                cid, label, GROUP_INFRA, RED,
                f"no heartbeat this session — last {_ago(inp.now, last)}", last,
            )
        return ComponentStatus(
            cid, label, GROUP_INFRA, YELLOW,
            f"stalled — heartbeat {_ago(inp.now, last)} "
            f"(expected ≤{DAEMON_STALE_S / 60:.0f} min during market hours)",
            last,
        )
    return ComponentStatus(
        cid, label, GROUP_INFRA, GRAY,
        f"market closed — last heartbeat {_ago(inp.now, last)}", last,
    )


def resolve_console_service(inp: FleetInputs) -> ComponentStatus:
    # Self-evident: this page rendering IS the console service being up.
    return ComponentStatus(
        "console_service", "Console (dashboard.service)", GROUP_INFRA,
        GREEN, "serving this page",
    )


def resolve_live_service(inp: FleetInputs) -> ComponentStatus:
    cid, label = "live_service", "Public live site (nous-ergon-live)"
    if inp.live_service_ok is None:
        return ComponentStatus(
            cid, label, GROUP_INFRA, GRAY, "probe n/a (not running on the dashboard box)",
        )
    if inp.live_service_ok:
        return ComponentStatus(cid, label, GROUP_INFRA, GREEN, "health probe OK")
    return ComponentStatus(
        cid, label, GROUP_INFRA, RED, "health probe failed — service down or unhealthy",
    )


def _pipeline_expectation(key: str, inp: FleetInputs) -> tuple[bool, Optional[datetime]]:
    """(expected_to_have_started_today, due_at_utc) for a pipeline."""
    now = inp.now
    if key == "weekly":
        if now.weekday() != 5:  # Saturday
            return False, None
        due = _utc_today_at(now, WEEKLY_CRON_UTC) + CRON_START_GRACE
        return now >= due, due
    if key == "preopen":
        if not inp.is_trading_day:
            return False, None
        due = _utc_today_at(now, PREOPEN_CRON_UTC) + CRON_START_GRACE
        return now >= due, due
    if key == "postclose":
        if not inp.is_trading_day:
            return False, None
        _, close_utc = market_hours_utc(now)
        due = close_utc + POSTCLOSE_DUE_LAG
        return now >= due, due
    return False, None


_PIPELINE_LABELS = {
    "weekly": "Weekly pipeline (ne-weekly-freshness)",
    "preopen": "Pre-open pipeline (ne-preopen-trading)",
    "postclose": "Post-close pipeline (ne-postclose-trading)",
}

# Human-readable cadence, surfaced in the idle reason string so a ⚪ dot
# reads as "on schedule" rather than "unexplained inactivity".
_PIPELINE_CADENCE = {
    "weekly": "runs weekly, Sat 09:00 UTC",
    "preopen": "runs weekdays, 12:45 UTC",
    "postclose": "runs weekdays, ~market close + 2h",
}


def resolve_pipeline(key: str, inp: FleetInputs) -> ComponentStatus:
    cid = f"pipeline_{key}"
    label = _PIPELINE_LABELS[key]
    cadence = _PIPELINE_CADENCE[key]
    snap: Optional[PipelineSnapshot] = inp.pipelines.get(key)

    if snap is None or snap.status == "UNAVAILABLE":
        err = (snap.error if snap else None) or "no data"
        return ComponentStatus(
            cid, label, GROUP_PIPELINES, YELLOW,
            f"Step Function status unavailable — {err}", deep_link="pipeline-status",
        )
    if snap.status == "NO_EXECUTIONS":
        return ComponentStatus(
            cid, label, GROUP_PIPELINES, GRAY, f"no executions yet ({cadence})",
            deep_link="pipeline-status",
        )
    if snap.status == "RUNNING":
        state_note = f" — {snap.current_state}" if snap.current_state else ""
        return ComponentStatus(
            cid, label, GROUP_PIPELINES, GREEN,
            f"running{state_note} (started {_ago(inp.now, snap.started_at)})",
            snap.started_at, deep_link="pipeline-status",
        )

    expected, due = _pipeline_expectation(key, inp)
    ran_today = (
        snap.started_at is not None and snap.started_at.date() == inp.now.date()
    )
    if expected and not ran_today:
        return ComponentStatus(
            cid, label, GROUP_PIPELINES, YELLOW,
            f"overdue — expected by {due.strftime('%H:%M')} UTC today; "
            f"last run {_ago(inp.now, snap.started_at)} ({cadence})",
            snap.started_at, deep_link="pipeline-status",
        )

    verdict = snap.verdict or ("COMPLETE" if snap.status == "SUCCEEDED" else "FAILED")
    when = _ago(inp.now, snap.stopped_at or snap.started_at)
    if verdict == "COMPLETE":
        return ComponentStatus(
            cid, label, GROUP_PIPELINES, GRAY,
            f"idle — last cycle COMPLETE ({when}); {cadence}",
            snap.stopped_at or snap.started_at, deep_link="pipeline-status",
        )
    if verdict == "PARTIAL":
        return ComponentStatus(
            cid, label, GROUP_PIPELINES, YELLOW,
            f"last cycle PARTIAL — some artifacts missing ({when})",
            snap.stopped_at or snap.started_at, deep_link="pipeline-status",
        )
    return ComponentStatus(
        cid, label, GROUP_PIPELINES, RED,
        f"last cycle FAILED ({when})",
        snap.stopped_at or snap.started_at, deep_link="pipeline-status",
    )


def resolve_groomer(inp: FleetInputs) -> ComponentStatus:
    """GREEN is reserved for ACTIVELY RUNNING only (Brian's call, 2026-07-08 —
    the prior scheme reused GREEN for both "running now" and "last run recent
    enough," which read as "it's running" when it was actually just idle-but-
    healthy — bit live when a last-run-7.8h-ago tile showed green during an
    unrelated manual test run). Four distinct states:
      GREEN  — a run is executing right now (fresh marker, or a live groom-spot
               instance the marker can't see, e.g. pre-marker driver code).
      GRAY   — idle, but the last run was recent enough to be unremarkable
               (within GROOM_IDLE_OK) — "nothing running, and that's fine."
      YELLOW — idle longer than expected for the 3×/day cadence, but not yet
               alarming (within GROOM_IDLE_WARN).
      RED    — busted: either a dangling in-progress marker with NO live spot
               to explain it (the run almost certainly died without
               finalizing), or idle well past the cadence (> GROOM_IDLE_WARN).
    """
    cid, label = "backlog_groomer", "Backlog groomer"
    g = inp.groom
    if g.marker_started_at is not None:
        marker_age = inp.now - g.marker_started_at
        tier = g.marker_tier or "?"
        model = g.marker_model or "?"
        if marker_age <= GROOM_MARKER_STALE:
            return ComponentStatus(
                cid, label, GROUP_JOBS, GREEN,
                f"running — {tier} tier ({model}), started {_ago(inp.now, g.marker_started_at)}",
                g.marker_started_at, deep_link="backlog-groom",
            )
        if not g.spot_running:
            return ComponentStatus(
                cid, label, GROUP_JOBS, RED,
                f"in-progress marker stale ({_ago(inp.now, g.marker_started_at)}) — "
                "run may have died without finalizing",
                g.marker_started_at, deep_link="backlog-groom",
            )
        # Stale marker but a groom spot is live: the marker is a leftover
        # from an earlier run; the running spot is the fresher truth.
    if g.spot_running:
        return ComponentStatus(
            cid, label, GROUP_JOBS, GREEN,
            "running — groom spot online since "
            f"{_ago(inp.now, g.spot_launched_at)} (no in-progress marker: "
            "pre-marker driver or marker write failed)",
            g.spot_launched_at, deep_link="backlog-groom",
        )
    if g.last_run_start is None:
        return ComponentStatus(
            cid, label, GROUP_JOBS, GRAY, "no groom run artifacts found",
            deep_link="backlog-groom",
        )
    idle = inp.now - g.last_run_start
    stop = f" ({g.last_stop_reason})" if g.last_stop_reason else ""
    if idle <= GROOM_IDLE_OK:
        return ComponentStatus(
            cid, label, GROUP_JOBS, GRAY,
            f"idle — last run {_ago(inp.now, g.last_run_start)}{stop}",
            g.last_run_start, deep_link="backlog-groom",
        )
    if idle <= GROOM_IDLE_WARN:
        return ComponentStatus(
            cid, label, GROUP_JOBS, YELLOW,
            f"last run {_ago(inp.now, g.last_run_start)} (cadence is 3×/day)",
            g.last_run_start, deep_link="backlog-groom",
        )
    return ComponentStatus(
        cid, label, GROUP_JOBS, RED,
        f"last run {_ago(inp.now, g.last_run_start)} (cadence is 3×/day)",
        g.last_run_start, deep_link="backlog-groom",
    )


def resolve_freshness_monitor(inp: FleetInputs) -> ComponentStatus:
    cid, label = "freshness_monitor", "Freshness monitor (Lambda)"
    hb = inp.heartbeat
    if not hb:
        return ComponentStatus(
            cid, label, GROUP_DATA, RED,
            "no heartbeat artifact (_freshness_monitor/heartbeat.json)",
            deep_link="artifact-freshness",
        )
    last_run = _parse_iso_utc(hb.get("last_run"))
    if last_run is None:
        return ComponentStatus(
            cid, label, GROUP_DATA, YELLOW, "heartbeat has no parseable last_run",
            deep_link="artifact-freshness",
        )
    age = (inp.now - last_run).total_seconds()
    mode = "alerts live" if hb.get("alerts_enabled") else "observe"
    if age <= FRESHNESS_HEARTBEAT_STALE_S:
        return ComponentStatus(
            cid, label, GROUP_DATA, GREEN,
            f"last sweep {_ago(inp.now, last_run)} ({mode})", last_run,
            deep_link="artifact-freshness",
        )
    if age <= FRESHNESS_HEARTBEAT_DEAD_S:
        return ComponentStatus(
            cid, label, GROUP_DATA, YELLOW,
            f"heartbeat aging — last sweep {_ago(inp.now, last_run)} "
            "(cadence 15 min)", last_run, deep_link="artifact-freshness",
        )
    return ComponentStatus(
        cid, label, GROUP_DATA, RED,
        f"monitor down — last sweep {_ago(inp.now, last_run)}", last_run,
        deep_link="artifact-freshness",
    )


def resolve_artifact_freshness(inp: FleetInputs) -> ComponentStatus:
    cid, label = "artifact_freshness", "Artifact freshness (fleet SLAs)"
    cr = inp.check_results
    if not cr or not isinstance(cr.get("results"), list):
        return ComponentStatus(
            cid, label, GROUP_DATA, GRAY, "no check_results artifact",
            deep_link="artifact-freshness",
        )
    results = cr["results"]
    bad_states = {"stale", "missing", "probe_failed"}
    not_fresh = [r for r in results if r.get("state") != "fresh"]
    critical_bad = [
        r for r in not_fresh
        if r.get("state") in bad_states and r.get("severity") == "critical"
    ]
    warning_bad = [r for r in not_fresh if r.get("state") in bad_states and r not in critical_bad]
    grace = [r for r in not_fresh if r.get("state") == "grace_period"]
    detail = tuple(
        {
            "artifact": r.get("artifact_id"),
            "state": r.get("state"),
            "severity": r.get("severity"),
            "owner": r.get("owner_repo"),
            "reason": (r.get("reason") or "")[:120],
        }
        for r in not_fresh
    )
    n = len(results)
    when = _parse_iso_utc(cr.get("run_at"))
    if critical_bad:
        return ComponentStatus(
            cid, label, GROUP_DATA, RED,
            f"{len(critical_bad)} critical artifact(s) past SLA "
            f"({len(not_fresh)}/{n} not fresh)",
            when, detail, deep_link="artifact-freshness",
        )
    if warning_bad or grace:
        return ComponentStatus(
            cid, label, GROUP_DATA, YELLOW,
            f"{len(warning_bad)} warning past SLA, {len(grace)} in grace "
            f"({n - len(not_fresh)}/{n} fresh)",
            when, detail, deep_link="artifact-freshness",
        )
    return ComponentStatus(
        cid, label, GROUP_DATA, GREEN, f"{n}/{n} artifacts fresh", when,
        deep_link="artifact-freshness",
    )


def resolve_module_self_reports(inp: FleetInputs) -> ComponentStatus:
    cid, label = "module_self_reports", "Module self-reports (health/*.json)"
    rows = inp.module_health
    if not rows:
        return ComponentStatus(cid, label, GROUP_DATA, GRAY, "no health artifacts")
    failed = [r for r in rows if r.status == "failed"]
    # Independent staleness check (config#1724: self-report is enrichment,
    # never authority) — a module that stopped running keeps its last "ok"
    # stamp forever; flag it as stale even though status still says fine.
    stale = [
        r for r in rows
        if r not in failed
        and r.stale_after_hrs is not None
        and r.age_hrs is not None
        and r.age_hrs > r.stale_after_hrs
    ]
    warn = [
        r for r in rows
        if r not in failed and r not in stale and r.status in ("degraded", "unknown")
    ]
    detail = tuple(
        {"module": r.module, "status": r.status,
         "age_hrs": None if r.age_hrs is None else round(r.age_hrs, 1),
         "stale_after_hrs": r.stale_after_hrs,
         "error": (r.error or "")[:120]}
        for r in rows
    )
    if failed:
        names = ", ".join(r.module for r in failed)
        return ComponentStatus(
            cid, label, GROUP_DATA, RED, f"failed: {names}", None, detail,
        )
    if stale:
        names = ", ".join(
            f"{r.module} ({r.age_hrs:.0f}h, SLA {r.stale_after_hrs:.0f}h)"
            for r in stale
        )
        return ComponentStatus(
            cid, label, GROUP_DATA, YELLOW,
            f"stale past SLA despite self-reported status: {names}", None, detail,
        )
    if warn:
        names = ", ".join(r.module for r in warn)
        return ComponentStatus(
            cid, label, GROUP_DATA, YELLOW, f"degraded/unknown: {names}", None, detail,
        )
    return ComponentStatus(
        cid, label, GROUP_DATA, GREEN, f"all {len(rows)} modules ok", None, detail,
    )


def _canary_escalation(age_hrs: Optional[float], now: datetime) -> Optional[tuple[str, str]]:
    """(dot, sentence) when the weekly canary drill (config#2223) demands
    escalation, else None (healthy heartbeat, or none expected yet). This is
    the "should have run and didn't" signal the last-fired display can't
    provide — see the CANARY_* constants for the tier rationale."""
    if age_hrs is None:
        if now >= CANARY_EXPECTED_FROM_UTC:
            return (
                RED,
                "canary drill has NEVER reported (weekly drill, config#2223) — "
                "the dispatch pipe is unverified and may be silently broken",
            )
        return None
    age = timedelta(hours=age_hrs)
    if age > CANARY_DEAD:
        return (
            RED,
            f"canary drill missed twice — last heartbeat {age_hrs / 24:.1f} d ago "
            "(weekly cadence); the dispatch pipe may be silently broken",
        )
    if age > CANARY_STALE:
        return (
            YELLOW,
            f"canary drill overdue — last heartbeat {age_hrs / 24:.1f} d ago "
            "(weekly cadence)",
        )
    return None


def _canary_detail(age_hrs: Optional[float]) -> tuple:
    """Additive expander row surfacing the canary freshness on EVERY dot
    state — the healthy-idle reason string stays byte-identical (config#2223
    acceptance: additive, not replacing)."""
    if age_hrs is None:
        return ()
    return (
        {
            "canary_last_heartbeat_days": round(age_hrs / 24, 1),
            "canary_cadence": "weekly drill (Wed, config#2223)",
        },
    )


def resolve_sf_watch(inp: FleetInputs) -> ComponentStatus:
    """Saturday SF Watch — fires only on a Saturday SF terminal failure
    (repository_dispatch, config#1227). Idle is the healthy steady state, so
    this is GRAY the vast majority of the time by design. It escalates to
    RED when the dispatch mechanism ITSELF is known to have broken (an open
    P1 issue from sf-watch.yml's own "dispatch failed to launch" failure
    report), and — since config#2223 — to YELLOW/RED when the weekly
    synthetic canary drill stops reporting, the signal that catches a
    SILENT break between real failures."""
    cid, label = "sf_watch", "Saturday SF Watch (resilience agent)"
    canary_detail = _canary_detail(inp.sf_watch_canary_age_hrs)
    # Live repair box outranks everything, including an open dispatch alert —
    # a box actively working IS the answer to "is the watch running right
    # now", and an alert is usually from the same incident it is fixing.
    if inp.sf_watch_box_running:
        since = (f" since {inp.sf_watch_box_launched_at}"
                 if inp.sf_watch_box_launched_at else "")
        return ComponentStatus(
            cid, label, GROUP_JOBS, GREEN,
            f"ACTIVE — repair box live{since}, working a failure now",
            deep_link="saturday-sf-watch",
        )
    if inp.sf_watch_alert:
        return ComponentStatus(
            cid, label, GROUP_JOBS, RED,
            f"dispatch alert open: {inp.sf_watch_alert}",
            detail=canary_detail, deep_link="saturday-sf-watch",
        )
    canary = _canary_escalation(inp.sf_watch_canary_age_hrs, inp.now)
    if canary is not None:
        dot, sentence = canary
        last = (
            f"; last real fire {inp.sf_watch_last_date}"
            if inp.sf_watch_last_date else "; no real fires recorded"
        )
        return ComponentStatus(
            cid, label, GROUP_JOBS, dot, f"{sentence}{last}",
            detail=canary_detail, deep_link="saturday-sf-watch",
        )
    if inp.sf_watch_last_date:
        return ComponentStatus(
            cid, label, GROUP_JOBS, GRAY,
            f"idle — last fired {inp.sf_watch_last_date} "
            f"({inp.sf_watch_last_n_events} event(s)); dispatch-driven, "
            "fires only on a Saturday SF failure",
            detail=canary_detail, deep_link="saturday-sf-watch",
        )
    return ComponentStatus(
        cid, label, GROUP_JOBS, GRAY,
        "no watch events recorded — dispatch-driven, fires only on a "
        "Saturday SF failure",
        detail=canary_detail, deep_link="saturday-sf-watch",
    )


def resolve_ci_watch(inp: FleetInputs) -> ComponentStatus:
    """Fleet CI Watch — fires only on a fleet repo's main-branch CI/deploy
    going red (repository_dispatch, config#1593). Same idle-is-healthy
    posture — and same config#2223 canary escalation — as
    :func:`resolve_sf_watch`; no dedicated console detail page yet, so no
    deep_link."""
    cid, label = "ci_watch", "Fleet CI Watch (resilience agent)"
    canary_detail = _canary_detail(inp.ci_watch_canary_age_hrs)
    # Same live-box-outranks-all posture as resolve_sf_watch.
    if inp.ci_watch_box_running:
        since = (f" since {inp.ci_watch_box_launched_at}"
                 if inp.ci_watch_box_launched_at else "")
        return ComponentStatus(
            cid, label, GROUP_JOBS, GREEN,
            f"ACTIVE — repair box live{since}, working a failure now",
        )
    if inp.ci_watch_alert:
        return ComponentStatus(
            cid, label, GROUP_JOBS, RED,
            f"dispatch alert open: {inp.ci_watch_alert}",
            detail=canary_detail,
        )
    canary = _canary_escalation(inp.ci_watch_canary_age_hrs, inp.now)
    if canary is not None:
        dot, sentence = canary
        last = (
            f"; last real fire {inp.ci_watch_last_date}"
            if inp.ci_watch_last_date else "; no real fires recorded"
        )
        return ComponentStatus(
            cid, label, GROUP_JOBS, dot, f"{sentence}{last}",
            detail=canary_detail,
        )
    if inp.ci_watch_last_date:
        return ComponentStatus(
            cid, label, GROUP_JOBS, GRAY,
            f"idle — last fired {inp.ci_watch_last_date} "
            f"({inp.ci_watch_last_n_events} event(s)); dispatch-driven, "
            "fires only on a fleet main-branch CI/deploy failure",
            detail=canary_detail,
        )
    return ComponentStatus(
        cid, label, GROUP_JOBS, GRAY,
        "no watch events recorded — dispatch-driven, fires only on a "
        "fleet main-branch CI/deploy failure",
        detail=canary_detail,
    )


def resolve_fleet(inp: FleetInputs) -> list[ComponentStatus]:
    """All components, in render order (grouped)."""
    return [
        resolve_trading_instance(inp),
        resolve_daemon(inp),
        resolve_console_service(inp),
        resolve_live_service(inp),
        resolve_pipeline("weekly", inp),
        resolve_pipeline("preopen", inp),
        resolve_pipeline("postclose", inp),
        resolve_groomer(inp),
        resolve_sf_watch(inp),
        resolve_ci_watch(inp),
        resolve_freshness_monitor(inp),
        resolve_artifact_freshness(inp),
        resolve_module_self_reports(inp),
    ]


def worst_dot(statuses: list[ComponentStatus]) -> str:
    """Severity rollup for the home-page strip (red > yellow > gray > green)."""
    if not statuses:
        return GRAY
    return max((s.dot for s in statuses), key=lambda d: _DOT_SEVERITY.get(d, 1))
