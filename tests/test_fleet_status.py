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
    EXERCISE_PIPELINE_ROLES,
    GRAY,
    GREEN,
    GROUP_ORDER,
    KNOWN_PIPELINE_ROLES,
    RECOVERY_PIPELINE_ROLES,
    RED,
    YELLOW,
    FleetInputs,
    GroomSnapshot,
    ModuleHealthRow,
    PipelineSnapshot,
    daemon_window,
    market_hours_utc,
    resolve_artifact_freshness,
    resolve_ci_watch,
    resolve_daemon,
    resolve_fleet,
    resolve_freshness_monitor,
    resolve_groomer,
    resolve_live_service,
    resolve_module_self_reports,
    resolve_pipeline,
    resolve_pipeline_role_coverage,
    resolve_sf_watch,
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

    def test_gray_skipped_is_neither_green_nor_red(self):
        """alpha-engine-config-I8069: a run that reached a declared no-op
        terminal (e.g. a THU/FRI WeeklyRunDaySkip) renders its own distinct
        state — never COMPLETE (a false green) and never FAILED (paging
        through working-as-designed behaviour)."""
        s = _pipe("weekly", PipelineSnapshot(
            status="SUCCEEDED", verdict="SKIPPED",
            started_at=SATURDAY - timedelta(hours=2),
            stopped_at=SATURDAY - timedelta(hours=1)),
            now=SATURDAY, trading=False)
        assert s.dot == GRAY
        assert s.dot != RED
        assert "skipped" in s.reason.lower()

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


class TestPipelineRecoveryRoles:
    """config#3085 — a failed scheduled cadence run followed by a
    watch-rerun/recovery/fast-path-rerun overlay must render the cycle's
    current truth, not the stale scheduled failure."""

    def test_running_recovery_role_is_green_and_named(self):
        # failed-sched+running-recovery: the loader's role-filter walk
        # already resolved to the newer recovery execution (RUNNING), so
        # this is what resolve_pipeline sees.
        s = _pipe("weekly", PipelineSnapshot(
            status="RUNNING", role="watch-rerun",
            started_at=TRADING_MID - timedelta(minutes=5)))
        assert s.dot == GREEN
        assert "running (recovery)" in s.reason

    def test_succeeded_recovery_role_is_green_recovered(self):
        # failed-sched+succeeded-recovery.
        s = _pipe("weekly", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="recovery",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == GREEN
        assert "recovered" in s.reason
        assert "recovery" in s.reason

    def test_failed_scheduled_no_recovery_stays_red(self):
        # failed-sched+no-recovery: role-filter walk found only the
        # scheduled cadence execution (still FAILED) — no recovery
        # overlay matched, so the dot stays RED as before.
        s = _pipe("weekly", PipelineSnapshot(
            status="FAILED", verdict="FAILED", role="weekly",
            started_at=TRADING_MID - timedelta(hours=2)))
        assert s.dot == RED
        assert "last cycle FAILED" in s.reason

    def test_fast_path_rerun_detected_by_name_not_role(self):
        # The Saturday-SF-watch dispatcher's fast-path rerun (live-verified
        # in infrastructure/lambdas/saturday-sf-watch-dispatcher/index.py,
        # nousergon-data) reuses the FAILED execution's own input verbatim,
        # so its pipeline_role is still the CADENCE role ("eod" here) — only
        # the execution name carries the "fast-path-rerun-" prefix.
        s = _pipe("postclose", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="eod",
            execution_name="fast-path-rerun-2026-07-20-093000",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == GREEN
        assert "recovered" in s.reason
        assert "fast-path rerun" in s.reason

    def test_cadence_role_execution_name_is_not_mistaken_for_recovery(self):
        # A first-try cadence execution's name never carries the
        # "fast-path-rerun-" prefix — sanity check the negative case.
        s = _pipe("postclose", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="eod",
            execution_name="ne-postclose-trading-pipeline-2026-07-20",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == GRAY
        assert "idle" in s.reason

    def test_first_try_success_is_gray_not_green(self):
        # A plain cadence success (role == the cadence role itself, not a
        # recovery overlay) keeps the existing idle-gray reading — only
        # recovery-role completions get the green "recovered" treatment.
        s = _pipe("weekly", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="weekly",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == GRAY
        assert "idle" in s.reason

    def test_recovery_roles_constant_excludes_smoke(self):
        assert "smoke" not in RECOVERY_PIPELINE_ROLES
        assert "shell-run" not in RECOVERY_PIPELINE_ROLES
        assert "backfill" not in RECOVERY_PIPELINE_ROLES
        assert "operator-replay" not in RECOVERY_PIPELINE_ROLES
        # fast-path-rerun is NOT a pipeline_role (it reuses the cadence
        # role — see test_fast_path_rerun_detected_by_name_not_role).
        assert {"watch-rerun", "recovery"} == set(RECOVERY_PIPELINE_ROLES)


# ── Daily EXERCISE cadence (config#5489 / #5520) ─────────────────────────────


def _weekly_pair(cadence, exercise, now=TRADING_MID, trading=True):
    """Resolve the weekly CADENCE row with an exercise snapshot present."""
    return resolve_pipeline(
        "weekly",
        _inputs(
            now=now,
            trading=trading,
            pipelines={"weekly": cadence, "weekly_exercise": exercise},
        ),
    )


class TestWeeklyExerciseRun:
    """The postclose-chained exercise run (pipeline_role="exercise") gets its
    OWN row and never displaces the Saturday cadence verdict."""

    def test_exercise_role_is_not_a_recovery_overlay(self):
        # The masking guard: were "exercise" a recovery role, a Tuesday
        # exercise SUCCESS would render the weekly cadence cycle COMPLETE
        # and hide a failed Saturday run.
        assert not (EXERCISE_PIPELINE_ROLES & RECOVERY_PIPELINE_ROLES)
        assert "exercise" in KNOWN_PIPELINE_ROLES

    def test_running_exercise_is_green_on_its_own_row(self):
        s = _pipe("weekly_exercise", PipelineSnapshot(
            status="RUNNING", role="exercise", current_state="RunResearch",
            started_at=TRADING_MID - timedelta(minutes=30)))
        assert s.dot == GREEN
        assert s.component_id == "pipeline_weekly_exercise"
        assert "RunResearch" in s.reason

    def test_failed_exercise_is_yellow_not_red(self):
        # The exercise cadence exists BECAUSE the pipeline is currently
        # broken; a red fleet rollup every trading day for the expected
        # state trains alarm-blindness. Named, not alarming.
        s = _pipe("weekly_exercise", PipelineSnapshot(
            status="FAILED", verdict="FAILED", role="exercise",
            started_at=TRADING_MID - timedelta(hours=3),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == YELLOW
        assert "last cycle FAILED" in s.reason

    def test_failed_weekly_cadence_stays_red(self):
        # Same snapshot on the CADENCE row keeps RED — a failed belief
        # refresh is a real outage.
        s = _pipe("weekly", PipelineSnapshot(
            status="FAILED", verdict="FAILED", role="weekly",
            started_at=TRADING_MID - timedelta(hours=3),
            stopped_at=TRADING_MID - timedelta(hours=1)))
        assert s.dot == RED

    def test_live_exercise_run_is_named_on_the_cadence_row(self):
        # The 2026-07-29 report, exactly: cadence run FAILED 3.8 d ago, an
        # exercise run RUNNING right now, and the console said only the
        # former. The verdict stays RED (the cadence really did fail) but
        # the reason must not read as "nothing has happened since".
        s = _weekly_pair(
            PipelineSnapshot(
                status="FAILED", verdict="FAILED", role="weekly",
                started_at=TRADING_MID - timedelta(days=3, hours=19),
                stopped_at=TRADING_MID - timedelta(days=3, hours=18)),
            PipelineSnapshot(
                status="RUNNING", role="exercise", current_state="RunResearch",
                started_at=TRADING_MID - timedelta(minutes=12)),
        )
        assert s.dot == RED
        assert "last cycle FAILED" in s.reason
        assert "EXERCISE run is RUNNING" in s.reason
        assert "RunResearch" in s.reason

    def test_newer_completed_exercise_is_named_but_does_not_clear_cadence(self):
        s = _weekly_pair(
            PipelineSnapshot(
                status="FAILED", verdict="FAILED", role="weekly",
                started_at=TRADING_MID - timedelta(days=4),
                stopped_at=TRADING_MID - timedelta(days=4)),
            PipelineSnapshot(
                status="SUCCEEDED", verdict="COMPLETE", role="exercise",
                started_at=TRADING_MID - timedelta(hours=5),
                stopped_at=TRADING_MID - timedelta(hours=1)),
        )
        assert s.dot == RED  # NOT cleared by the exercise success
        assert "newer daily EXERCISE run COMPLETE" in s.reason

    def test_older_exercise_run_adds_no_note(self):
        s = _weekly_pair(
            PipelineSnapshot(
                status="SUCCEEDED", verdict="COMPLETE", role="weekly",
                started_at=TRADING_MID - timedelta(hours=2),
                stopped_at=TRADING_MID - timedelta(hours=1)),
            PipelineSnapshot(
                status="FAILED", verdict="FAILED", role="exercise",
                started_at=TRADING_MID - timedelta(days=2),
                stopped_at=TRADING_MID - timedelta(days=2)),
        )
        assert s.dot == GRAY
        assert "EXERCISE" not in s.reason

    def test_no_exercise_snapshot_leaves_cadence_reason_untouched(self):
        snap = PipelineSnapshot(
            status="FAILED", verdict="FAILED", role="weekly",
            started_at=TRADING_MID - timedelta(days=4),
            stopped_at=TRADING_MID - timedelta(days=4))
        assert _pipe("weekly", snap).reason == _weekly_pair(
            snap, PipelineSnapshot(status="NO_EXECUTIONS")).reason

    def test_exercise_overdue_is_anchored_to_the_last_session_close(self):
        # Mon 2026-07-06 close (20:00 UTC) + 2h postclose + 2h margin ⇒ the
        # exercise run for Monday's session is due by Tue 00:00 UTC. At
        # TRADING_MID (Tue 15:00 UTC) that deadline is long past.
        prev_close = datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)

        def _ex(snap):
            return resolve_pipeline("weekly_exercise", _inputs(
                pipelines={"weekly_exercise": snap},
                prev_session_close_utc=prev_close))

        # Ran after Monday's close — on schedule.
        ok = _ex(PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="exercise",
            started_at=prev_close + timedelta(minutes=53),
            stopped_at=prev_close + timedelta(hours=4)))
        assert ok.dot == GRAY

        # Newest run predates Monday's close ⇒ Monday's exercise never ran.
        # This is the case a same-UTC-day comparison could never catch: the
        # deadline lands at midnight, after the day it belongs to has rolled.
        missed = _ex(PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="exercise",
            started_at=prev_close - timedelta(days=1),
            stopped_at=prev_close - timedelta(days=1)))
        assert missed.dot == YELLOW
        assert "overdue" in missed.reason

    def test_exercise_expectation_declines_without_a_session_anchor(self):
        # prev_session_close_utc=None (loader could not resolve one) must
        # degrade to "not expected" — never a false overdue.
        s = _pipe("weekly_exercise", PipelineSnapshot(
            status="SUCCEEDED", verdict="COMPLETE", role="exercise",
            started_at=TRADING_MID - timedelta(days=9),
            stopped_at=TRADING_MID - timedelta(days=9)))
        assert s.dot == GRAY
        assert "overdue" not in s.reason


class TestPipelineRoleCoverage:
    """The total classifier over pipeline_role (config#5590): a role minted
    by a producer that no row here classifies must be LOUD, not silent."""

    def test_green_when_every_role_is_classified(self):
        s = resolve_pipeline_role_coverage(_inputs())
        assert s.dot == GREEN
        assert s.component_id == "pipeline_role_coverage"

    def test_yellow_names_the_unrecognized_role_and_state_machine(self):
        s = resolve_pipeline_role_coverage(_inputs(unrecognized_roles=(
            ("ne-weekly-freshness-pipeline", "exercise-v2",
             "2026-07-07T14:00:00+00:00"),
        )))
        assert s.dot == YELLOW
        assert "exercise-v2" in s.reason
        assert "ne-weekly-freshness-pipeline" in s.reason
        assert s.detail[0]["pipeline_role"] == "exercise-v2"

    def test_known_roles_cover_the_producer_vocabulary(self):
        # Every role the fleet's producers mint today. A new one added to a
        # producer repo without landing here is what this row detects.
        assert {"weekly", "daily", "eod"} <= KNOWN_PIPELINE_ROLES
        assert RECOVERY_PIPELINE_ROLES <= KNOWN_PIPELINE_ROLES
        assert EXERCISE_PIPELINE_ROLES <= KNOWN_PIPELINE_ROLES
        assert {"smoke", "shell-run", "backfill", "operator-replay"} <= (
            KNOWN_PIPELINE_ROLES
        )


# ── Groomer ─────────────────────────────────────────────────────────────────


class TestGroomer:
    def test_green_in_progress_marker(self):
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(minutes=30),
            marker_tier="high", marker_model="claude-opus-4-8")))
        assert s.dot == GREEN
        assert "running" in s.reason
        assert "high" in s.reason

    def test_red_stale_marker_no_spot_is_busted(self):
        # config#1954 follow-up (Brian, 2026-07-08): GREEN is reserved for
        # ACTIVELY RUNNING only. A dangling in-progress marker with no live
        # spot to explain it means the run almost certainly died without
        # finalizing — that's "busted," not a soft warning.
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(hours=5))))
        assert s.dot == RED
        assert "stale" in s.reason

    def test_gray_recent_run_no_marker_is_idle_not_running(self):
        # Nothing is executing right now — this is "idle, and that's fine"
        # (within GROOM_IDLE_OK), never GREEN (reserved for actively running).
        s = resolve_groomer(_inputs(groom=GroomSnapshot(
            last_run_start=TRADING_MID - timedelta(hours=2),
            last_stop_reason="queue drained")))
        assert s.dot == GRAY

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


# ── SF Watch / CI Watch ──────────────────────────────────────────────────────


class TestSfWatch:
    def test_gray_no_events_ever(self):
        s = resolve_sf_watch(_inputs())
        assert s.dot == GRAY
        assert "no watch events recorded" in s.reason

    def test_green_active_when_repair_box_live(self):
        # A running repair box is the "working right now" signal and outranks
        # everything else — including an open dispatch alert, which is usually
        # from the same incident the box is fixing (2026-07-11).
        s = resolve_sf_watch(_inputs(
            sf_watch_box_running=True,
            sf_watch_box_launched_at="2026-07-11T14:29:51+00:00",
            sf_watch_alert="dispatch failed to launch for x"))
        assert s.dot == GREEN
        assert "ACTIVE" in s.reason
        assert "2026-07-11T14:29:51+00:00" in s.reason

    def test_green_active_without_launch_time(self):
        s = resolve_sf_watch(_inputs(sf_watch_box_running=True))
        assert s.dot == GREEN
        assert "ACTIVE" in s.reason

    def test_gray_idle_with_last_fired(self):
        # Idle-with-history is still the healthy steady state — dispatch-
        # driven, so no fire since last Saturday is expected, not stale.
        s = resolve_sf_watch(_inputs(
            sf_watch_last_date="2026-07-04", sf_watch_last_n_events=2))
        assert s.dot == GRAY
        assert "2026-07-04" in s.reason
        assert "2 event(s)" in s.reason

    def test_red_open_dispatch_alert(self):
        # The one thing this component can catch without a live failure to
        # trigger it: sf-watch.yml's own "dispatch failed to launch" issue.
        s = resolve_sf_watch(_inputs(
            sf_watch_alert="SF-watch dispatch failed to launch for x (2026-07-11)"))
        assert s.dot == RED
        assert "dispatch alert open" in s.reason

    def test_deep_links_to_saturday_sf_watch_page(self):
        assert resolve_sf_watch(_inputs()).deep_link == "saturday-sf-watch"


class TestCiWatch:
    def test_gray_no_events_ever(self):
        s = resolve_ci_watch(_inputs())
        assert s.dot == GRAY
        assert "no watch events recorded" in s.reason

    def test_green_active_when_repair_box_live(self):
        # Same live-box-outranks-all posture as TestSfWatch.
        s = resolve_ci_watch(_inputs(
            ci_watch_box_running=True,
            ci_watch_box_launched_at="2026-07-11T13:18:25+00:00",
            ci_watch_alert="dispatch failed for nousergon/x"))
        assert s.dot == GREEN
        assert "ACTIVE" in s.reason

    def test_gray_idle_with_last_fired(self):
        s = resolve_ci_watch(_inputs(
            ci_watch_last_date="2026-07-10", ci_watch_last_n_events=1))
        assert s.dot == GRAY
        assert "2026-07-10" in s.reason

    def test_red_open_dispatch_alert(self):
        s = resolve_ci_watch(_inputs(
            ci_watch_alert="CI-watch dispatch failed to launch for nousergon/x"))
        assert s.dot == RED
        assert "dispatch alert open" in s.reason


# ── Full resolve + rollup ───────────────────────────────────────────────────


class TestResolveFleet:
    def test_returns_all_components_in_known_groups(self):
        # 13 → 14: `fleet_checks` added (config-I5548).
        # 14 → 16 (config#5590): `pipeline_weekly_exercise` — the weekly SF
        # now runs a SECOND cadence (postclose-chained exercise runs,
        # config#5489) whose verdict must not be folded into the Saturday
        # cadence row — and `pipeline_role_coverage`, the total classifier
        # that makes the next unmodelled pipeline_role loud instead of
        # silent. The pinned count is deliberate — it forces a conscious
        # call whenever the Fleet Status row set changes, since adding rows
        # is how a status page stops being readable. `fleet_checks` is ONE
        # rolled-up row for every scheduled check precisely to keep that
        # number from growing per check.
        statuses = resolve_fleet(_inputs())
        assert len(statuses) == 16
        assert {s.group for s in statuses} <= set(GROUP_ORDER)
        assert len({s.component_id for s in statuses}) == 16

    def test_worst_dot_severity_order(self):
        statuses = resolve_fleet(_inputs(trading_instance_state="stopped"))
        assert worst_dot(statuses) == RED
        assert worst_dot([]) == GRAY


# ── Watch-dispatch canary drill (config#2223) ────────────────────────────────
# Frozen clocks for the canary tiers: the weekly drill ships 2026-07-11 with
# CANARY_EXPECTED_FROM_UTC = 2026-07-23, so TRADING_MID (2026-07-07) sits in
# the benign pre-ship window and POST_SHIP well after it.

POST_SHIP = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


class TestSfWatchCanary:
    def test_healthy_canary_keeps_idle_reason_byte_identical(self):
        # Acceptance (config#2223): additive, not replacing — a fresh
        # heartbeat must not change the existing last-fired reason string.
        # Base reason from the pre-ship clock (canary makes no claim there);
        # the gray idle reason itself is clock-independent.
        base = resolve_sf_watch(_inputs(
            sf_watch_last_date="2026-07-04", sf_watch_last_n_events=2))
        with_canary = resolve_sf_watch(_inputs(
            now=POST_SHIP, sf_watch_last_date="2026-07-04",
            sf_watch_last_n_events=2, sf_watch_canary_age_hrs=48.0))
        assert with_canary.dot == GRAY
        assert with_canary.reason == base.reason
        # The canary freshness surfaces additively, in the detail rows.
        assert with_canary.detail == (
            {"canary_last_heartbeat_days": 2.0,
             "canary_cadence": "weekly drill (Wed, config#2223)"},
        )

    def test_yellow_one_missed_weekly_drill(self):
        s = resolve_sf_watch(_inputs(
            now=POST_SHIP, sf_watch_last_date="2026-07-04",
            sf_watch_canary_age_hrs=9 * 24.0))
        assert s.dot == YELLOW
        assert "canary drill overdue" in s.reason
        assert "last real fire 2026-07-04" in s.reason

    def test_red_two_consecutive_missed_drills(self):
        s = resolve_sf_watch(_inputs(
            now=POST_SHIP, sf_watch_canary_age_hrs=16 * 24.0))
        assert s.dot == RED
        assert "canary drill missed twice" in s.reason
        assert "no real fires recorded" in s.reason

    def test_red_never_reported_once_canary_is_due(self):
        # The "missing entirely after ship" leg: no heartbeat has EVER been
        # written and the first drills are overdue — a real "should have run
        # and didn't", unlike the pre-ship window below.
        s = resolve_sf_watch(_inputs(now=POST_SHIP))
        assert s.dot == RED
        assert "NEVER reported" in s.reason

    def test_benign_before_expected_from_when_never_reported(self):
        # TRADING_MID (2026-07-07) predates CANARY_EXPECTED_FROM_UTC — no
        # false RED on a not-yet-applied schedule; reason unchanged.
        s = resolve_sf_watch(_inputs())
        assert s.dot == GRAY
        assert "no watch events recorded" in s.reason

    def test_exactly_at_stale_boundary_is_still_gray(self):
        s = resolve_sf_watch(_inputs(
            now=POST_SHIP, sf_watch_canary_age_hrs=8 * 24.0))
        assert s.dot == GRAY

    def test_open_dispatch_alert_outranks_canary_escalation(self):
        s = resolve_sf_watch(_inputs(
            now=POST_SHIP,
            sf_watch_alert="SF-watch dispatch failed to launch for x",
            sf_watch_canary_age_hrs=9 * 24.0))
        assert s.dot == RED
        assert "dispatch alert open" in s.reason

    def test_live_repair_box_outranks_canary_escalation(self):
        # A live box IS proof the pipe works right now — a stale canary must
        # not repaint an actively-working watch.
        s = resolve_sf_watch(_inputs(
            now=POST_SHIP, sf_watch_box_running=True,
            sf_watch_canary_age_hrs=16 * 24.0))
        assert s.dot == GREEN
        assert "ACTIVE" in s.reason

    def test_canary_escalation_deep_links_to_watch_page(self):
        s = resolve_sf_watch(_inputs(
            now=POST_SHIP, sf_watch_canary_age_hrs=9 * 24.0))
        assert s.deep_link == "saturday-sf-watch"


class TestCiWatchCanary:
    def test_healthy_canary_keeps_idle_reason_byte_identical(self):
        # Base reason from the pre-ship clock — see the sf sibling test.
        base = resolve_ci_watch(_inputs(
            ci_watch_last_date="2026-07-10", ci_watch_last_n_events=1))
        with_canary = resolve_ci_watch(_inputs(
            now=POST_SHIP, ci_watch_last_date="2026-07-10",
            ci_watch_last_n_events=1, ci_watch_canary_age_hrs=48.0))
        assert with_canary.dot == GRAY
        assert with_canary.reason == base.reason
        assert with_canary.detail == (
            {"canary_last_heartbeat_days": 2.0,
             "canary_cadence": "weekly drill (Wed, config#2223)"},
        )

    def test_yellow_one_missed_weekly_drill(self):
        s = resolve_ci_watch(_inputs(
            now=POST_SHIP, ci_watch_last_date="2026-07-10",
            ci_watch_canary_age_hrs=9 * 24.0))
        assert s.dot == YELLOW
        assert "canary drill overdue" in s.reason
        assert "last real fire 2026-07-10" in s.reason

    def test_red_two_consecutive_missed_drills(self):
        s = resolve_ci_watch(_inputs(
            now=POST_SHIP, ci_watch_canary_age_hrs=16 * 24.0))
        assert s.dot == RED
        assert "canary drill missed twice" in s.reason

    def test_red_never_reported_once_canary_is_due(self):
        s = resolve_ci_watch(_inputs(now=POST_SHIP))
        assert s.dot == RED
        assert "NEVER reported" in s.reason

    def test_benign_before_expected_from_when_never_reported(self):
        s = resolve_ci_watch(_inputs())
        assert s.dot == GRAY
        assert "no watch events recorded" in s.reason

    def test_open_dispatch_alert_outranks_canary_escalation(self):
        s = resolve_ci_watch(_inputs(
            now=POST_SHIP,
            ci_watch_alert="CI-watch dispatch failed to launch for nousergon/x",
            ci_watch_canary_age_hrs=9 * 24.0))
        assert s.dot == RED
        assert "dispatch alert open" in s.reason

    def test_live_repair_box_outranks_canary_escalation(self):
        s = resolve_ci_watch(_inputs(
            now=POST_SHIP, ci_watch_box_running=True,
            ci_watch_canary_age_hrs=16 * 24.0))
        assert s.dot == GREEN
