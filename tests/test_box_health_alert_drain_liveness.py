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


def _fake_aws(tmp_path: Path, list_objects_output: str, list_rc: int = 0) -> Path:
    """A stub `aws` covering only `s3api list-objects-v2`, the one subcommand
    check_alert_drain_liveness calls. Any other invocation is a test bug."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # The canned output is written to a sibling FILE and `cat` from there,
    # rather than embedded as a shell literal, so a tab byte in the AWS
    # `--output text` format survives verbatim instead of being re-escaped by
    # a layer of shell quoting.
    fixture = tmp_path / "aws_output.txt"
    fixture.write_text(list_objects_output)
    script = bin_dir / "aws"
    script.write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        if [ "$1" = "s3api" ] && [ "$2" = "list-objects-v2" ]; then
            cat "{fixture}"
            exit {list_rc}
        fi
        echo "unexpected aws invocation: $*" >&2
        exit 99
        """)
    )
    script.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, list_objects_output: str, now_epoch: int, list_rc: int = 0) -> str:
    bin_dir = _fake_aws(tmp_path, list_objects_output, list_rc)
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


def test_fresh_completion_marker_is_silent(tmp_path):
    fresh_epoch = 1787270400  # 2026-08-21T00:00:00Z
    now_epoch = fresh_epoch + 3 * 3600  # 3h old, well under the 14h ceiling
    out = _run(
        tmp_path,
        "overseer/_control/completed/alert-drain-drain-2026-08-21T0000Z.json\t2026-08-21T00:00:00.000Z",
        now_epoch=now_epoch,
    )
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
