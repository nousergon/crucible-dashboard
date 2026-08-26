"""check_alert_drain_liveness — detect a drain that is scheduled-off or hung.

WHY THIS EXISTS
----------------
Investigating alpha-engine-config-I7858 (whether the box-health `warning`
tier may move off the alert channel) surfaced a standing gap independent of
how that question was answered: the existing
`alpha-engine-alert-drain-liveness-probe` (nousergon-data) only relaunches a
DEAD spot box. It has no opinion on a drain that never launches at all
(schedule disabled) or one that launches and exits `success` without
consuming anything. I7858 itself was closed by `crucible-dashboard-PR758`,
which reclassified the dominant `warning` contributor (`memory budget:
BREACH`) to `info` rather than moving the whole tier off channel — but this
check stands on its own regardless of that routing decision, since the
alert-drain's own liveness was never covered either way.

This function reads the drain's own completion marker in
`s3://alpha-engine-research/overseer/_control/completed/` rather than
querying SQS or EventBridge Scheduler directly, because this box's IAM role
(alpha-engine-dashboard-role) has S3 read access to that bucket already and
no SQS/Scheduler permissions — granting those is an IAM change that belongs
to nous-ergon-ops, not this repo.

These tests run the SHIPPED function, extracted by name from box_health.sh,
against a stubbed `aws` on PATH — not a reimplementation of its logic.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH_PATH = REPO_ROOT / "infrastructure" / "box_health.sh"
BOX_HEALTH = BOX_HEALTH_PATH.read_text()


def _bash() -> str:
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if Path(candidate).exists():
            return candidate
    pytest.skip("no bash available")


def _fake_aws(
    tmp_path: Path,
    list_objects_output: str,
    list_rc: int = 0,
    marker_body: str | None = None,
    schedule_states: "list[str] | None" = None,
) -> Path:
    """A stub `aws` covering `s3api list-objects-v2`, the `s3 cp <key> -` that
    reads the completion marker's body (alpha-engine-config-I8108), and
    `scheduler get-schedule` (alpha-engine-config-I8679). Any other invocation
    is a test bug.

    `marker_body=None` makes the body read return EMPTY, which is the shape of
    a marker this box cannot read.

    `schedule_states` is consumed IN THE ORDER box_health.sh queries the four
    schedule names, one line each. A state of `FAIL` makes that one call exit
    non-zero with no output — the IAM-drift shape. `None` defaults to all four
    DISABLED, which is the live state as of the 2026-08-23 pause."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # The canned output is written to a sibling FILE and `cat` from there,
    # rather than embedded as a shell literal, so a tab byte in the AWS
    # `--output text` format survives verbatim instead of being re-escaped by
    # a layer of shell quoting.
    fixture = tmp_path / "aws_output.txt"
    fixture.write_text(list_objects_output)
    marker_fixture = tmp_path / "marker.json"
    marker_fixture.write_text(marker_body or "")
    # One line per schedule query, popped in order by a counter file. A
    # positional counter rather than a name->state map on purpose: the ORDER
    # the four names are queried in is part of what a mixed-state test asserts.
    states = schedule_states if schedule_states is not None else ["DISABLED"] * 4
    sched_fixture = tmp_path / "schedule_states.txt"
    sched_fixture.write_text("\n".join(states) + "\n")
    sched_counter = tmp_path / "schedule_calls.txt"
    script = bin_dir / "aws"
    script.write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        if [ "$1" = "s3api" ] && [ "$2" = "list-objects-v2" ]; then
            cat "{fixture}"
            exit {list_rc}
        fi
        if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
            cat "{marker_fixture}"
            exit 0
        fi
        if [ "$1" = "scheduler" ] && [ "$2" = "get-schedule" ]; then
            n=$(cat "{sched_counter}" 2>/dev/null || echo 0)
            n=$((n + 1))
            echo "$n" > "{sched_counter}"
            state=$(sed -n "${{n}}p" "{sched_fixture}")
            if [ "$state" = "FAIL" ]; then exit 254; fi
            echo "$state"
            exit 0
        fi
        echo "unexpected aws invocation: $*" >&2
        exit 99
        """)
    )
    script.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path,
    list_objects_output: str,
    now_epoch: int,
    list_rc: int = 0,
    marker_body: str | None = None,
    schedule_states: "list[str] | None" = None,
) -> str:
    bin_dir = _fake_aws(
        tmp_path, list_objects_output, list_rc, marker_body, schedule_states
    )
    script = (
        f'set -u\n'
        f'PATH="{bin_dir}:$PATH"\n'
        f'OVERSEER_RESEARCH_BUCKET=alpha-engine-research\n'
        f'ALERT_DRAIN_MAX_STALENESS_H=14\n'
        f'ALERT_DRAIN_PAUSE_REVIEW_DAYS=14\n'
        f'ALERT_DRAIN_SCHEDULE_NAMES="a b c d"\n'
        # A hand-written ISO-8601 parser, not `command date -d`: this suite
        # must pass on macOS (BSD date, no -d) as well as the box (GNU date,
        # where the shipped code runs unmodified) — python3's stdlib parser
        # is the portable common ground for the TEST STUB only.
        f'date() {{\n'
        f'  if [ "$1" = "+%s" ]; then echo {now_epoch}\n'
        f'  elif [ "$1" = "-d" ]; then\n'
        f'    python3 -c "import sys,datetime as d; ts=sys.argv[1].split(\\".\\")[0].rstrip(\\"Z\\"); '
        f'print(int(d.datetime.strptime(ts, \\"%Y-%m-%dT%H:%M:%S\\").replace(tzinfo=d.timezone.utc).timestamp()))" "$2" 2>/dev/null\n'
        f'  else command date "$@"; fi\n'
        f'}}\n'
        f'source <(sed -n "/^alert_drain_declared_state() {{/,/^}}/p" "{BOX_HEALTH_PATH}")\n'
        f'source <(sed -n "/^check_alert_drain_liveness() {{/,/^}}/p" "{BOX_HEALTH_PATH}")\n'
        f'check_alert_drain_liveness\n'
    )
    return subprocess.run(
        [_bash(), "-c", script], capture_output=True, text=True
    ).stdout


def test_stale_completion_marker_pages_critical(tmp_path):
    """No completed run in >14h reads as scheduled-off or hung."""
    # 2026-08-21T00:00:00Z; "now" set 20h later below.
    stale_epoch = 1787270400  # 2026-08-21T00:00:00Z
    now_epoch = stale_epoch + 20 * 3600
    out = _run(
        tmp_path,
        "overseer/_control/completed/alert-drain-drain-2026-08-21T0000Z.json\t2026-08-21T00:00:00.000Z",
        now_epoch=now_epoch,
        # ENABLED: the drain is SUPPOSED to be running. That is the only state
        # in which a stale marker is an incident (alpha-engine-config-I8679).
        schedule_states=["ENABLED"] * 4,
    )
    assert "alert-drain not consuming: " in out
    assert "ENABLED" in out
    assert "alpha-engine-config-I7858" in out


HEALTHY_MARKER = (
    '{"state":"success","rc":0,"run_id":"drain-2026-08-21T2200Z",'
    '"at":"2026-08-21T22:31:00Z","run_log_s3_uri":"",'
    '"queue_depth_before":25,"ingested":{"queue":25,"fallback":0}}'
)
LISTING = ("overseer/_control/completed/alert-drain-drain-2026-08-21T0000Z.json"
           "\t2026-08-21T00:00:00.000Z")


def test_fresh_completion_marker_is_silent(tmp_path):
    fresh_epoch = 1787270400  # 2026-08-21T00:00:00Z
    now_epoch = fresh_epoch + 3 * 3600  # 3h old, well under the 14h ceiling
    out = _run(tmp_path, LISTING, now_epoch=now_epoch, marker_body=HEALTHY_MARKER)
    assert out.strip() == "", f"expected silence for a fresh marker, got: {out!r}"


def test_empty_listing_is_a_watchdog_finding_not_silence(tmp_path):
    """A listing failure (IAM drift, throttle, or genuinely zero runs ever)
    must not be indistinguishable from health — it is reported, not skipped,
    same class as the df/timer probes elsewhere in this file."""
    out = _run(tmp_path, "None", now_epoch=1755734400)
    assert "watchdog: cannot read alert-drain completion markers" in out


def test_finding_classifies_as_critical():
    """The backstop cannot share a tier with what it backstops (I7858)."""
    assert '"alert-drain not consuming: "*) echo critical ;;' in BOX_HEALTH


def test_function_is_wired_into_snapshot_problems():
    # Called, not merely defined — one definition site, at least one bare
    # call site (`    check_alert_drain_liveness` with no trailing `()`,
    # which is how bash invokes it and how box_health.sh's other checks —
    # e.g. http_liveness_problems — are called from snapshot_problems).
    assert BOX_HEALTH.count("check_alert_drain_liveness() {") == 1, (
        "expected exactly one definition of check_alert_drain_liveness"
    )
    assert "\n    check_alert_drain_liveness\n" in BOX_HEALTH, (
        "check_alert_drain_liveness is defined but never called from "
        "snapshot_problems — a check nobody calls is not a check"
    )


# ── Zero-ingest-from-a-non-empty-queue (alpha-engine-config-I8108) ───────────
#
# The staleness check above catches scheduled-off, hung and dead. It cannot
# catch a run that fires, completes `state:success` on time, and silently
# consumes NOTHING from a non-empty queue — the marker lands exactly when it
# should. Every case below uses a FRESH marker, so the staleness branch is
# silent and only this assertion can speak. That separation is the point: if a
# future edit made these markers stale, the tests would pass for the wrong
# reason.

_FRESH_EPOCH = 1787270400          # 2026-08-21T00:00:00Z (the marker's mtime)
_NOW = _FRESH_EPOCH + 3 * 3600     # 3h old — well inside the 14h ceiling


def _marker(depth: str, ingested: str) -> str:
    return (
        '{"state":"success","rc":0,"run_id":"drain-2026-08-21T2200Z",'
        '"at":"2026-08-21T22:31:00Z","run_log_s3_uri":"",'
        f'"queue_depth_before":{depth},"ingested":{ingested}}}'
    )


def test_zero_ingested_from_a_non_empty_queue_pages(tmp_path):
    """The gap this closes: 25 messages waiting, 0 consumed, exit success."""
    out = _run(tmp_path, LISTING, now_epoch=_NOW,
               marker_body=_marker("25", '{"queue":0,"fallback":0}'))
    assert "alert-drain not consuming: " in out
    assert "alpha-engine-config-I8108" in out


def test_zero_ingested_from_an_empty_queue_is_silent(tmp_path):
    """A genuinely quiet 6-hour window. Paging here is what made alerting on
    `ingested == 0` alone unusable, and it is why the queue depth is needed."""
    out = _run(tmp_path, LISTING, now_epoch=_NOW,
               marker_body=_marker("0", '{"queue":0,"fallback":0}'))
    assert out.strip() == "", f"expected silence for a quiet cycle, got: {out!r}"


def test_partial_consumption_is_silent(tmp_path):
    """Anything above zero means the consumer path works. A backlog left behind
    is the drain's own `backlog_remaining` signal, not this check's business."""
    out = _run(tmp_path, LISTING, now_epoch=_NOW,
               marker_body=_marker("500", '{"queue":500,"fallback":0}'))
    assert out.strip() == ""


def test_unmeasured_depth_is_a_watchdog_finding_not_health(tmp_path):
    """`null` — the producer could not read SQS. That is coverage blindness, and
    it must never render as either a quiet cycle or a healthy run. This is the
    exact "could not check" -> "checked and fine" collapse the fleet hit four
    separate times this week."""
    out = _run(tmp_path, LISTING, now_epoch=_NOW,
               marker_body=_marker("null", '{"queue":0,"fallback":0}'))
    assert "watchdog: alert-drain completion marker carries no queue-depth" in out
    assert "not consuming" not in out


def test_unmeasured_ingested_is_a_watchdog_finding_not_health(tmp_path):
    """`ingest` never ran, so nothing knows what was consumed."""
    out = _run(tmp_path, LISTING, now_epoch=_NOW, marker_body=_marker("25", "null"))
    assert "watchdog: alert-drain completion marker carries no queue-depth" in out


def test_a_marker_predating_the_producer_change_is_a_watchdog_finding(tmp_path):
    """The pre-I8108 marker shape. Reported as unverified coverage, never as
    health — and it clears on its own once the producer half deploys."""
    legacy = ('{"state":"success","rc":0,"run_id":"drain-2026-08-21T2200Z",'
              '"at":"2026-08-21T22:31:00Z","run_log_s3_uri":""}')
    out = _run(tmp_path, LISTING, now_epoch=_NOW, marker_body=legacy)
    assert "watchdog: alert-drain completion marker carries no queue-depth" in out


def test_an_unreadable_marker_body_is_a_watchdog_finding(tmp_path):
    out = _run(tmp_path, LISTING, now_epoch=_NOW, marker_body="")
    assert "watchdog: cannot read the alert-drain completion marker body" in out


def test_a_canary_drill_marker_is_never_asserted_on(tmp_path):
    """A drill deliberately exits before the charter and never touches the
    queue. Asserting on its counts would page on every successful drill."""
    drill = ('{"state":"drill","rc":0,"run_id":"drain-drill-2026-08-21T0400Z",'
             '"at":"2026-08-21T04:00:00Z","run_log_s3_uri":"",'
             '"queue_depth_before":null,"ingested":null}')
    out = _run(tmp_path, LISTING, now_epoch=_NOW, marker_body=drill)
    assert out.strip() == "", f"expected silence for a drill marker, got: {out!r}"


def test_a_stale_marker_reports_staleness_only_not_both(tmp_path):
    """One condition, one line. A stale marker's contents say nothing useful
    about consumption, and reporting both would double-count the incident."""
    out = _run(tmp_path, LISTING, now_epoch=_FRESH_EPOCH + 20 * 3600,
               marker_body=_marker("25", '{"queue":0,"fallback":0}'),
               schedule_states=["ENABLED"] * 4)
    # Static since alpha-engine-config-I8678 — the age lives in the journal.
    assert "no completed run within the staleness bound while all four schedules are ENABLED" in out
    assert "I8108" not in out


def test_the_zero_ingest_finding_classifies_as_critical():
    """It shares the `alert-drain not consuming: ` prefix deliberately: the
    condition is the same one from the operator's side — the drain is not
    consuming — and that prefix is already tiered critical, outside the tier
    whose delivery depends on the drain itself."""
    assert '"alert-drain not consuming: "*) echo critical ;;' in BOX_HEALTH
    assert "silent consumption failure" in BOX_HEALTH


def test_the_unmeasured_finding_classifies_as_warning_not_critical():
    """Coverage blindness about the watchdog is a `watchdog:` finding
    (warning), per overseer-policy section 3 — recorded and swept, never paged.
    It must not join the critical the drain itself owns."""
    assert '"watchdog: "*) echo warning ;;' in BOX_HEALTH


# ── identity stability (alpha-engine-config-I8678) ──────────────────────────
#
# `publish_clears` derives this tier's identity key from the problem SET, and
# says so: "a set that changed at all is a different page". A problem string
# carrying a live relative age therefore opens a NEW condition every hour and
# ends the previous one — one CRITICAL plus one RESOLVED per hour, forever,
# with the RESOLVED naming an age exactly one hour behind its CRITICAL. That
# was the observed 2026-08-26 storm. Same reasoning that kept computed relative
# age out of the timer identity key (alpha-engine-config-I7677) and out of the
# I8108 sibling arm.

def test_stale_marker_line_is_identical_across_ages(tmp_path):
    """The SAME standing condition must produce a byte-identical line at 19h,
    20h and 40h. This is the regression test for the hourly page/clear pair."""
    stale_epoch = 1787270400  # 2026-08-21T00:00:00Z
    listing = (
        "overseer/_control/completed/alert-drain-drain-2026-08-21T0000Z.json"
        "\t2026-08-21T00:00:00.000Z"
    )
    for states, prefix in ((["ENABLED"] * 4, "alert-drain not consuming: "),
                           (["DISABLED"] * 4, "notice: alert-drain not consuming")):
        lines = set()
        for age_h in (19, 20, 40):
            case_dir = tmp_path / f"{states[0]}-{age_h}"
            case_dir.mkdir(parents=True, exist_ok=True)
            out = _run(case_dir, listing,
                       now_epoch=stale_epoch + age_h * 3600, schedule_states=states)
            found = [ln for ln in out.splitlines() if ln.startswith(prefix)]
            assert found, f"no {prefix!r} line at {age_h}h: {out!r}"
            lines.add(found[0])
        assert len(lines) == 1, (
            f"line moves with age, so every tick is a new page/row: {lines}"
        )


def test_stale_marker_line_carries_no_interpolation(tmp_path):
    """The key moves too — a new completion marker while the drain is still
    off would re-key the page just as an age does. Neither belongs in the
    string; both belong in the journal."""
    stale_epoch = 1787270400
    now_epoch = stale_epoch + 20 * 3600
    for key in ("alert-drain-drain-2026-08-21T0000Z.json", "alert-drain-drain-2026-08-25T1201Z.json"):
        case_dir = tmp_path / key.replace(".json", "")
        case_dir.mkdir(parents=True, exist_ok=True)
        out = _run(
            case_dir,
            f"overseer/_control/completed/{key}\t2026-08-21T00:00:00.000Z",
            now_epoch=now_epoch,
            schedule_states=["ENABLED"] * 4,
        )
        stale = [ln for ln in out.splitlines() if ln.startswith("alert-drain not consuming: ")]
        assert stale, out
        assert key not in stale[0], f"marker key leaked into the problem string: {stale[0]}"
        assert "20h" not in stale[0]


# Moving quantities this file computes. A problem string that interpolates one
# of these cannot hold a stable identity key across ticks. Stable
# interpolations (a systemd unit name, a service name) are deliberately absent
# from this list — I7677 established that identity may name WHAT, never HOW
# LONG or HOW MUCH.
MOVING_QUANTITIES = ("age_h", "now_epoch", "disk_pct", "mem_avail_mb", "depth", "ingested")


def test_no_problem_string_interpolates_a_moving_quantity():
    """Sweep, not instance (engagement-protocol-policy section 5): the defect
    class is 'a live measurement inside a problem string', and fixing only the
    alert-drain line would leave the next one to be found by a phone."""
    offenders = []
    for lineno, line in enumerate(BOX_HEALTH.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith('echo "'):
            continue
        # Problem strings are the ones publish_problems/classify_problem_severity
        # match on; journal lines go through printf ... >&2, never echo.
        if stripped.endswith('>&2'):
            continue
        for var in MOVING_QUANTITIES:
            if "${%s}" % var in stripped or "$%s" % var in stripped:
                offenders.append(f"{lineno}: {stripped}")
                break
    assert not offenders, (
        "problem strings carrying a live measurement — each one re-keys its page "
        "on every tick (alpha-engine-config-I8678):\n" + "\n".join(offenders)
    )


# ── paused is not an incident (alpha-engine-config-I8679) ───────────────────
#
# Brian ruling 2026-08-26: "i don't want to be paged with box health at all if
# there is no issue." The four alert-drain schedules have been DISABLED since
# 2026-08-23 22:37 UTC by his own ruling, recorded in nousergon-data
# infrastructure/automation_pause.json. Until this split, a stale completion
# marker paged `critical` whether the drain was hung or deliberately off — the
# message even said "scheduled-off or hung", two answers collapsed in the
# direction that pages.

_STALE_EPOCH = 1787270400  # 2026-08-21T00:00:00Z
_STALE_LISTING = (
    "overseer/_control/completed/alert-drain-drain-2026-08-21T0000Z.json"
    "\t2026-08-21T00:00:00.000Z"
)


def _run_stale(tmp_path, age_h, states, name="case"):
    case_dir = tmp_path / f"{name}-{age_h}"
    case_dir.mkdir(parents=True, exist_ok=True)
    return _run(
        case_dir,
        _STALE_LISTING,
        now_epoch=_STALE_EPOCH + age_h * 3600,
        schedule_states=states,
    )


def test_declared_off_never_reaches_the_page_tier(tmp_path):
    """All four DISABLED -> a `notice:` line, which classify_problem_severity
    routes to `info` and emit_hygiene_envelope renders on the console. It must
    NOT carry the `alert-drain not consuming: ` prefix, because that prefix is
    what the classifier matches on to page `critical`."""
    out = _run_stale(tmp_path, 20, ["DISABLED"] * 4)
    assert out.startswith("notice: "), out
    assert "DECLARED OFF" in out
    assert not any(
        ln.startswith("alert-drain not consuming: ") for ln in out.splitlines()
    ), f"a declared-off drain reached the paging tier: {out!r}"


def test_declared_off_is_reported_not_silenced(tmp_path):
    """principles.md section 7 — a paused producer renders as a visibly-
    not-green row, never as nothing. Suppressing the page must not suppress
    the finding."""
    out = _run_stale(tmp_path, 20, ["DISABLED"] * 4).strip()
    assert out, "a declared-off drain produced NO finding at all — unobserved, not healthy"


def test_declared_off_past_the_review_bound_says_a_decision_is_owed(tmp_path):
    """A pause is a state; a 14-day pause is a decision the operator owes. The
    row changes text at the bound. It still does not page: a decision owed is
    Decision-Queue work, not a box-health incident."""
    before = _run_stale(tmp_path, 13 * 24, ["DISABLED"] * 4, name="before")
    after = _run_stale(tmp_path, 15 * 24, ["DISABLED"] * 4, name="after")
    assert "decision owed" not in before, before
    assert "decision owed" in after, after
    assert after.startswith("notice: "), f"the lapse must not page: {after!r}"


def test_enabled_and_stale_still_pages(tmp_path):
    """The finding this check exists for is untouched. A drain that is
    SUPPOSED to be running and has produced no completed marker is an
    incident, and the split must not have quietly removed that."""
    out = _run_stale(tmp_path, 20, ["ENABLED"] * 4)
    assert out.startswith("alert-drain not consuming: "), out
    assert "hung or crashed" in out


def test_mixed_schedule_state_is_a_watchdog_finding(tmp_path):
    """Neither paused nor running. Two of four disabled is config drift that
    no other check on this box would see, and it is NOT the pause Brian
    declared — so it must not inherit the pause's silence."""
    out = _run_stale(tmp_path, 20, ["DISABLED", "ENABLED", "DISABLED", "DISABLED"])
    assert out.startswith("watchdog: "), out
    assert "disagree" in out


def test_unreadable_schedule_state_is_a_watchdog_finding_not_a_default(tmp_path):
    """UNKNOWN IS NOT DISABLED. If GetSchedule fails — IAM drift, throttle, a
    renamed schedule — defaulting to `disabled` would silence a genuinely hung
    drain the moment this call broke, which is the failure mode that makes a
    monitor worse than none."""
    out = _run_stale(tmp_path, 20, ["FAIL", "DISABLED", "DISABLED", "DISABLED"])
    assert out.startswith("watchdog: "), out
    assert "unmeasured" in out
    assert "DECLARED OFF" not in out


def test_all_four_schedules_are_queried():
    """The state is derived from all four, not sampled from one — the mixed
    case is only reachable if every name is asked."""
    assert BOX_HEALTH.count("alpha-engine-alert-drain-") >= 4
    for hhmm in ("0400", "1000", "1600", "2200"):
        assert f"alpha-engine-alert-drain-{hhmm}utc" in BOX_HEALTH


def test_notice_arm_precedes_the_drain_critical_arm_in_the_classifier():
    """`notice: ` and `alert-drain not consuming: ` are both matched by
    classify_problem_severity's case statement, and bash takes the FIRST
    match. If the drain arm ever moves above the notice arm, every paused-drain
    row silently becomes a page again — the exact regression this PR fixes,
    reintroduced by an unrelated edit."""
    notice_at = BOX_HEALTH.index('"notice: "*) echo info ;;')
    drain_at = BOX_HEALTH.index('"alert-drain not consuming: "*) echo critical ;;')
    assert notice_at < drain_at, (
        "the drain arm now precedes the notice arm; a declared-off drain would page"
    )
