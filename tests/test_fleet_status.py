"""Frozen-clock tests for the Fleet Status resolver (fleet_status.py).

The resolver is pure — every green/yellow/red/gray verdict is a
deterministic function of a FleetInputs snapshot — so the full status
matrix is exercised here without AWS, S3, or a live clock.

Reference clocks (2026, EDT — market 13:30–20:00 UTC):
  TRADING_MID   Tue 2026-07-07 15:00 UTC — mid-session on a trading day
  TRADING_EARLY Tue 2026-07-07 11:00 UTC — trading day, before the pre-open window
  SATURDAY      Sat 2026-07-11 10:00 UTC — after the weekly 09:00 cron (+grace)
  SUNDAY        Sun 2026-07-12 15:00 UTC — non-trading day
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fleet_status import (  # noqa: E402
    GRAY,
    GREEN,
    GROUP_ORDER,
    RED,
    YELLOW,
    FleetInputs,
    GroomSnapshot,
    ModuleHealthRow,
    PipelineSnapshot,
    daemon_window,
    market_hours_utc,
    resolve_artifact_freshness,
    resolve_daemon,
    resolve_fleet,
    resolve_freshness_monitor,
    resolve_groomer,
    resolve_live_service,
    resolve_module_self_reports,
    resolve_pipeline,
    resolve_trading_instance,
    trading_instance_window,
    worst_dot,
)

TRADING_MID = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
TRADING_EARLY = datetime(2026, 7, 7, 11, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)


def _inputs(now=TRADING_MID, trading=True, **kw) -> FleetInputs:
    return FleetInputs(now=now, is_trading_day=trading, **kw)


# ── Window math ──────────────────────────────────────────────────────────────


class TestWindows:
    def test_market_hours_edt(self):
        open_utc, close_utc = market_hours_utc(TRADING_MID)
        assert open_utc == datetime(2026, 7, 7, 13, 30, tzinfo=timezone.utc)
        assert close_utc == datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)

    def test_market_hours_est_no_dst_skew(self):
        # January (EST): open is 14:30 UTC — a fixed-UTC anchor would be wrong.
        jan = datetime(2026, 1, 6, 16, 0, tzinfo=timezone.utc)
        open_utc, close_utc = market_hours_utc(jan)
        assert open_utc.hour == 14 and open_utc.minute == 30
        assert close_utc.hour == 21

    def test_instance_window_spans_preopen_to_post_close(self):
        start, end = trading_instance_window(TRADING_MID)
        assert start == datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc)  # 12:45+15m
        assert end == datetime(2026, 7, 7, 21, 15, tzinfo=timezone.utc)  # close+75m

    def test_daemon_window_inside_market_hours(self):
        start, end = daemon_window(TRADING_MID)
        assert start == datetime(2026, 7, 7, 13, 35, tzinfo=timezone.utc)
        assert end == datetime(2026, 7, 7, 20, 5, tzinfo=timezone.utc)


# ── Trading instance ────────────────────────────────────────────────────────


class TestTradingInstance:
    def test_green_running_online(self):
        s = resolve_trading_instance(_inputs(
            trading_instance_state="running", trading_instance_ping="Online"))
        assert s.dot == GREEN

    def test_yellow_running_ping_lost(self):
        # The 7/6 wedge signature: instance up, SSM agent unreachable.
        s = resolve_trading_instance(_inputs(
            trading_instance_state="running",
            trading_instance_ping="ConnectionLost"))
        assert s.dot == YELLOW
        assert "ConnectionLost" in s.reason

    def test_green_running_ping_unknown(self):
        s = resolve_trading_instance(_inputs(
            trading_instance_state="running", trading_instance_ping=None))
        assert s.dot == GREEN

    def test_red_stopped_in_window(self):
        s = resolve_trading_instance(_inputs(trading_instance_state="stopped"))
        assert s.dot == RED

    def test_gray_stopped_outside_window(self):
        s = resolve_trading_instance(_inputs(
            now=TRADING_EARLY, trading_instance_state="stopped"))
        assert s.dot == GRAY

    def test_gray_stopped_non_trading_day(self):
        s = resolve_trading_instance(_inputs(
            now=SUNDAY, trading=False, trading_instance_state="stopped"))
        assert s.dot == GRAY
        assert "market closed" in s.reason

    def test_green_running_outside_window_notes_it(self):
        s = resolve_trading_instance(_inputs(
            now=TRADING_EARLY, trading_instance_state="running",
            trading_instance_ping="Online"))
        assert s.dot == GREEN
        assert "outside scheduled" in s.reason

    def test_ec2_unavailable_yellow_when_expected(self):
        s = resolve_trading_instance(_inputs(
            ec2_available=False, ec2_error="AccessDenied"))
        assert s.dot == YELLOW
        assert "AccessDenied" in s.reason

    def test_ec2_unavailable_gray_when_not_expected(self):
        s = resolve_trading_instance(_inputs(
            now=SUNDAY, trading=False, ec2_available=False, ec2_error="x"))
        assert s.dot == GRAY


# ── Daemon ──────────────────────────────────────────────────────────────────


class TestDaemon:
    def test_green_fresh_heartbeat(self):
        s = resolve_daemon(_inputs(intraday_nav_age_s=60.0))
        assert s.dot == GREEN

    def test_yellow_stalled_within_session(self):
        # Last write 15 min ago, still after today's open ⇒ stalled.
        s = resolve_daemon(_inputs(intraday_nav_age_s=900.0))
        assert s.dot == YELLOW
        assert "stalled" in s.reason

    def test_red_heartbeat_predates_session(self):
        # Last write 2h ago (13:00 UTC) < session start ⇒ never came up today.
        s = resolve_daemon(_inputs(intraday_nav_age_s=7200.0))
        assert s.dot == RED
        assert "no heartbeat this session" in s.reason

    def test_red_missing_in_market_hours(self):
        s = resolve_daemon(_inputs(intraday_nav_age_s=None))
        assert s.dot == RED

    def test_gray_off_hours(self):
        s = resolve_daemon(_inputs(now=TRADING_EARLY, intraday_nav_age_s=7200.0))
        assert s.dot == GRAY

    def test_gray_non_trading_day(self):
        s = resolve_daemon(_inputs(now=SUNDAY, trading=False,
                                   intraday_nav_age_s=100000.0))
        assert s.dot == GRAY


# ── Live service ────────────────────────────────────────────────────────────


class TestLiveService:
    def test_green(self):
        assert resolve_live_service(_inputs(live_service_ok=True)).dot == GREEN

    def test_red(self):
        assert resolve_live_service(_inputs(live_service_ok=False)).dot == RED

    def test_gray_probe_unavailable(self):
        assert resolve_live_service(_inputs(live_service_ok=None)).dot == GRAY


# ── Pipelines ───────────────────────────────────────────────────────────────


def _pipe(key, snap, now=TRADING_MID, trading=True):
    return resolve_pipeline(key, _inputs(now=now, trading=trading,
                                         pipelines={key: snap}))


class TestPipelines:
    def test_green_running_names_current_state(self):
        s = _pipe("preopen", PipelineSnapshot(
            status="RUNNING", started_at=TRADING_MID - timedelta(minutes=20),
            current_state="RunMorningPlanner"))
        assert s.dot == GREEN
        assert "RunMorningPlanner" in s.reason

    def test_gray_idle_complete(self):
        # Idle between scheduled runs is the pipeline's normal state — a
        # completed cycle with nothing due reads ⚪, not 🟢 (🟢 means an
        # execution is actually in flight right now).
        s = _pipe("preopen", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == GRAY
        assert "runs weekdays" in s.reason

    def test_yellow_partial(self):
        s = _pipe("preopen", PipelineSnapshot(
            status="FAILED", verdict="PARTIAL",
            started_at=TRADING_MID - timedelta(hours=2)))
        assert s.dot == YELLOW

    def test_red_failed(self):
        s = _pipe("preopen", PipelineSnapshot(
            status="FAILED", verdict="FAILED",
            started_at=TRADING_MID - timedelta(hours=2)))
        assert s.dot == RED

    def test_yellow_preopen_overdue(self):
        # 15:00 UTC on a trading day, newest run started yesterday.
        s = _pipe("preopen", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=TRADING_MID - timedelta(days=1)))
        assert s.dot == YELLOW
        assert "overdue" in s.reason

    def test_gray_preopen_before_cron(self):
        s = _pipe("preopen", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=TRADING_EARLY - timedelta(days=1)), now=TRADING_EARLY)
        assert s.dot == GRAY

    def test_yellow_weekly_overdue_saturday(self):
        s = _pipe("weekly", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=SATURDAY - timedelta(days=7)),
            now=SATURDAY, trading=False)
        assert s.dot == YELLOW

    def test_gray_weekly_idle_midweek(self):
        s = _pipe("weekly", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=TRADING_MID - timedelta(days=3)))
        assert s.dot == GRAY
        assert "runs weekly" in s.reason

    def test_yellow_postclose_overdue(self):
        late = datetime(2026, 7, 7, 22, 30, tzinfo=timezone.utc)  # close+2h = 22:00
        s = _pipe("postclose", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=late - timedelta(days=1)), now=late)
        assert s.dot == YELLOW

    def test_gray_postclose_not_yet_due(self):
        s = _pipe("postclose", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE",
            started_at=TRADING_MID - timedelta(days=1)))
        assert s.dot == GRAY

    def test_yellow_unavailable(self):
        s = _pipe("preopen", PipelineSnapshot(status="UNAVAILABLE", error="throttled"))
        assert s.dot == YELLOW
        assert "throttled" in s.reason

    def test_yellow_missing_snapshot(self):
        s = resolve_pipeline("preopen", _inputs(pipelines={}))
        assert s.dot == YELLOW

    def test_gray_no_executions(self):
        s = _pipe("preopen", PipelineSnapshot(status="NO_EXECUTIONS"))
        assert s.dot == GRAY


# ── Groomer ─────────────────────────────────────────────────────────────────


class TestGroomer:
    def test_green_in_progress_marker(self):
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(minutes=30),
            marker_tier="high", marker_model="claude-opus-4-8")))
        assert s.dot == GREEN
        assert "running" in s.reason
        assert "high" in s.reason

    def test_yellow_stale_marker(self):
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(hours=5))))
        assert s.dot == YELLOW
        assert "stale" in s.reason

    def test_green_recent_run_no_marker(self):
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            last_run_start=TRADING_MID - timedelta(hours=2),
            last_stop_reason="queue drained")))
        assert s.dot == GREEN

    def test_yellow_idle_15h(self):
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            last_run_start=TRADING_MID - timedelta(hours=15))))
        assert s.dot == YELLOW

    def test_red_idle_40h(self):
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            last_run_start=TRADING_MID - timedelta(hours=40))))
        assert s.dot == RED

    def test_gray_no_artifacts(self):
        assert resolve_groomer(_inputs()).dot == GRAY

    def test_green_spot_running_without_marker(self):
        # A run launched on pre-marker driver code (or whose marker write
        # failed): the live groom spot is the independent control-plane
        # signal — bit live 2026-07-06 (Opus run invisible to the page).
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            last_run_start=TRADING_MID - timedelta(hours=12),
            spot_running=True,
            spot_launched_at=TRADING_MID - timedelta(minutes=25))))
        assert s.dot == GREEN
        assert "groom spot online" in s.reason

    def test_green_spot_running_overrides_stale_marker(self):
        # Leftover active marker from a crashed earlier run + a live spot:
        # the running spot is the fresher truth.
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(hours=6),
            spot_running=True,
            spot_launched_at=TRADING_MID - timedelta(minutes=10))))
        assert s.dot == GREEN

    def test_fresh_marker_wins_over_spot(self):
        # Marker carries tier/model detail — preferred when fresh.
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(minutes=20),
            marker_tier="high", marker_model="claude-opus-4-8",
            spot_running=True,
            spot_launched_at=TRADING_MID - timedelta(minutes=25))))
        assert s.dot == GREEN
        assert "high" in s.reason


# ── Freshness monitor + artifact rollup ─────────────────────────────────────


def _hb(age_min: float, alerts=True) -> dict:
    return {
        "last_run": (TRADING_MID - timedelta(minutes=age_min)).isoformat(),
        "alerts_enabled": alerts,
    }


class TestFreshnessMonitor:
    def test_green_recent_sweep(self):
        assert resolve_freshness_monitor(_inputs(heartbeat=_hb(5))).dot == GREEN

    def test_yellow_aging(self):
        assert resolve_freshness_monitor(_inputs(heartbeat=_hb(40))).dot == YELLOW

    def test_red_dead(self):
        assert resolve_freshness_monitor(_inputs(heartbeat=_hb(120))).dot == RED

    def test_red_missing_heartbeat(self):
        assert resolve_freshness_monitor(_inputs(heartbeat=None)).dot == RED

    def test_yellow_unparseable_last_run(self):
        s = resolve_freshness_monitor(_inputs(heartbeat={"last_run": "garbage"}))
        assert s.dot == YELLOW


def _cr(rows) -> dict:
    return {"run_at": TRADING_MID.isoformat(), "results": rows}


def _row(state, severity="warning", artifact="a1"):
    return {"artifact_id": artifact, "state": state, "severity": severity,
            "owner_repo": "r", "reason": ""}


class TestArtifactFreshness:
    def test_green_all_fresh(self):
        s = resolve_artifact_freshness(_inputs(
            check_results=_cr([_row("fresh"), _row("fresh", artifact="a2")])))
        assert s.dot == GREEN

    def test_red_critical_missing(self):
        s = resolve_artifact_freshness(_inputs(check_results=_cr(
            [_row("fresh"), _row("missing", severity="critical", artifact="a2")])))
        assert s.dot == RED
        assert s.detail  # non-fresh rows exposed for the expander

    def test_yellow_warning_stale(self):
        s = resolve_artifact_freshness(_inputs(check_results=_cr(
            [_row("fresh"), _row("stale", artifact="a2")])))
        assert s.dot == YELLOW

    def test_yellow_grace_only(self):
        s = resolve_artifact_freshness(_inputs(check_results=_cr(
            [_row("fresh"), _row("grace_period", severity="critical", artifact="a2")])))
        assert s.dot == YELLOW

    def test_gray_missing_artifact(self):
        assert resolve_artifact_freshness(_inputs(check_results=None)).dot == GRAY


# ── Module self-reports ─────────────────────────────────────────────────────


class TestModuleSelfReports:
    def test_green_all_ok(self):
        s = resolve_module_self_reports(_inputs(module_health=(
            ModuleHealthRow("research", "ok"), ModuleHealthRow("executor", "ok"))))
        assert s.dot == GREEN

    def test_yellow_degraded(self):
        s = resolve_module_self_reports(_inputs(module_health=(
            ModuleHealthRow("research", "ok"),
            ModuleHealthRow("executor", "degraded"))))
        assert s.dot == YELLOW
        assert "executor" in s.reason

    def test_red_failed(self):
        s = resolve_module_self_reports(_inputs(module_health=(
            ModuleHealthRow("research", "failed"),)))
        assert s.dot == RED

    def test_yellow_stale_despite_ok_status(self):
        # A writer that died silently leaves its last "ok" stamp in place
        # forever — the independent age check must catch this even though
        # self-reported status never flagged anything (config#1724).
        s = resolve_module_self_reports(_inputs(module_health=(
            ModuleHealthRow("executor", "ok", age_hrs=200.0, stale_after_hrs=96.0),
        )))
        assert s.dot == YELLOW
        assert "executor" in s.reason

    def test_green_within_sla_despite_long_cadence(self):
        s = resolve_module_self_reports(_inputs(module_health=(
            ModuleHealthRow("research", "ok", age_hrs=50.0, stale_after_hrs=192.0),
        )))
        assert s.dot == GREEN

    def test_gray_empty(self):
        assert resolve_module_self_reports(_inputs()).dot == GRAY


# ── Full resolve + rollup ───────────────────────────────────────────────────


class TestResolveFleet:
    def test_returns_all_components_in_known_groups(self):
        statuses = resolve_fleet(_inputs())
        assert len(statuses) == 11
        assert {s.group for s in statuses} <= set(GROUP_ORDER)
        assert len({s.component_id for s in statuses}) == 11

    def test_worst_dot_severity_order(self):
        statuses = resolve_fleet(_inputs(trading_instance_state="stopped"))
        assert worst_dot(statuses) == RED
        assert worst_dot([]) == GRAY
