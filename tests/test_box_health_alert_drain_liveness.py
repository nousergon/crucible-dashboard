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
    tmp_path: Path, list_objects_output: str, list_rc: int = 0, marker_body: str | None = None
) -> Path:
    """A stub `aws` covering `s3api list-objects-v2` and the `s3 cp <key> -`
    that reads the completion marker's body (alpha-engine-config-I8108). Any
    other invocation is a test bug.

    `marker_body=None` makes the body read return EMPTY, which is the shape of
    a marker this box cannot read."""
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
) -> str:
    bin_dir = _fake_aws(tmp_path, list_objects_output, list_rc, marker_body)
    script = (
        f'set -u\n'
        f'PATH="{bin_dir}:$PATH"\n'
        f'OVERSEER_RESEARCH_BUCKET=alpha-engine-research\n'
        f'ALERT_DRAIN_MAX_STALENESS_H=14\n'
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
    )
    assert "alert-drain not consuming: " in out
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
               marker_body=_marker("25", '{"queue":0,"fallback":0}'))
    assert "no completed run in 20h" in out
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
