"""`health_problems_unalerted` is the series the CloudWatch backstop alarms on.

WHY THIS METRIC EXISTS (alpha-engine-config-I8035)
--------------------------------------------------
`publish_verdict` emits `AlphaEngine/Box::health_problems` as a LEVEL — the
current critical count, republished every 10-minute tick. Two of its inputs are
themselves levels: `timer job failing:` is derived from
`systemctl show -p Result`, which stays `exit-code` until the unit's NEXT run.
So one failing DAILY timer holds the level for ~144 consecutive ticks.

Measured on i-09b539c844515d549 across 274 watchdog runs, 2026-08-07..21:

    timer job failing: ops-config-drift.timer     175 ticks  <-  3 actual failures

and `alpha-engine-dashboard-health-problems` transitioned ALARM<->OK 15 times in
13 days. That alarm has both `AlarmActions` and `OKActions` on the
alpha-engine-alerts SNS topic, so 3 timer failures cost 30 emails — each one
arriving after `krepis.alerts` had already delivered the same finding.

The alert path solved this in config-I7677: `timer_failure_dedup_key` keys a
timer finding on (unit, Result, InactiveExitTimestamp) so one failing RUN pages
once. The metric path never inherited it, deliberately — config-I5211 built it
as an INDEPENDENT path precisely so a broken alerts module could not hide a
finding, and reaching into the alert path's state would have undone that.

The resolution keeps both. `health_problems` stays an unconditional level with
no alarm action. `health_problems_unalerted` counts only criticals whose
`krepis.alerts publish` FAILED, and the alarm moves onto it — which is the
condition the alarm's own description has always claimed to watch:

    "box_health.sh confirmed >=1 problem it may not have been able to alert
     about (config-I5211)"

I5211's case survives: the failure it was built for (a guard-less silent-no-op
alerts module, the config#1646 class) fails EVERY publish, so this metric goes
non-zero and the alarm fires.

Source-text assertions, matching the other box_health.sh guards: the script runs
as root against systemd and CloudWatch on an EC2 box, and executing it in CI is
not meaningful. What is pinned here is the contract.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SH = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()


def test_the_metric_is_published():
    assert "MetricName=health_problems_unalerted" in SH


def test_every_exit_path_publishes_it():
    """Including the healthy ones — principles.md §7.

    A series that stops must stay distinguishable from a healthy zero. There
    are exactly three terminal paths: the no-problems-found early exit, the
    all-self-healed early exit, and the tail after every publish has been
    attempted.
    """
    assert SH.count("publish_unalerted 0") == 2, (
        "both clean-exit paths must publish an explicit 0; a series that simply "
        "stops on a healthy box is indistinguishable from a dead collector"
    )
    assert 'publish_unalerted "$UNALERTED_CRITICALS"' in SH, (
        "the problem path must publish the final failure count"
    )


def test_it_is_published_after_the_alert_attempts():
    """Ordering is the whole contract, and it is the OPPOSITE of publish_verdict's.

    `publish_verdict` runs BEFORE alerting so a failing alert path cannot stop
    the count being recorded (test_verdict_is_published_before_alerting).
    `publish_unalerted` must run AFTER, because until every critical publish has
    been attempted the counter is not final — publishing it earlier would emit a
    count of failures that had not happened yet.
    """
    last_publish_call = SH.rindex("-m krepis.alerts publish")
    final_emit = SH.rindex('publish_unalerted "$UNALERTED_CRITICALS"')
    assert final_emit > last_publish_call


def test_only_criticals_are_counted():
    """A warning that fails to publish is still on the console.

    `emit_hygiene_envelope` renders the warning and notice tiers unconditionally,
    so a failed publish there is not an undelivered finding. Counting them would
    make the backstop fire for conditions that ARE visible, which is the class of
    over-firing this whole issue is about.
    """
    assert 'if [ "$severity" = "critical" ]; then' in SH
    # The count moved into publish_page with the console-routing split
    # (alpha-engine-config-I9044) and is passed in as `count`; asserted INSIDE
    # that function so the guard cannot be satisfied by prose elsewhere.
    import re as _re

    body = _re.search(r"^publish_page\(\) \{.*?^\}", SH, _re.M | _re.S)
    assert body, "publish_page() not found — the krepis publish moved again"
    assert "UNALERTED_CRITICALS=$((UNALERTED_CRITICALS + count))" in body.group(0)
    assert 'if [ "$severity" = "critical" ]; then' in body.group(0), (
        "the critical-only guard is no longer beside the count it gates"
    )


def test_the_counter_starts_at_zero_before_any_publish():
    """Otherwise `set -u` aborts the run, taking every check down with it.

    That is not hypothetical on this file: an unset variable reaching arithmetic
    under `set -u` is exactly how the timer last-trigger parse killed an entire
    snapshot on 2026-07-28 (see classify_timer's comment).
    """
    init = SH.index("UNALERTED_CRITICALS=0")
    first_increment = SH.index("UNALERTED_CRITICALS=$((")
    assert init < first_increment


def test_publish_problems_still_reports_failures_to_the_journal():
    """Counting must not have replaced the journal line.

    The metric says HOW MANY went undelivered; only the journal says which tier
    and, with the surrounding context, which finding. Losing the line would make
    a non-zero metric undiagnosable.
    """
    assert 'echo "box_health: $severity publish failed" >&2' in SH
