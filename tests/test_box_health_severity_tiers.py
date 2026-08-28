"""box_health.sh's three severity tiers — who gets woken, and who does not.

WHY
---
krepis.alerts pushes a phone notification for `error`/`critical` only.
`warning` is delivered silently in-channel and still reaches SNS and the
Overseer intake bus.

`info` no longer goes to krepis.alerts at all (2026-08-20). "Silently" there
means `disable_notification=True`, which suppresses the phone push and NOT the
message -- so every info line still landed in Brian's chat. The tier's findings
now publish to the console's fleet-check surface instead; see
`emit_box_health_hygiene.py` and test_box_health_hygiene_console_routing.py.

Two tiers could not express the box's actual states. Before 2026-07-29
everything published at `warning`, so a service being DOWN was as quiet as a
censored memory reading. That was fixed by pushing everything that was not a
`notice: ` line — which re-broke it in the other direction. Measured over
2026-07-27..2026-08-03 from the S3 dedup markers: box-health produced 96 of ~138
fleet alert publishes (~70%), essentially all of it pushing, and the single
largest contributor was `memory budget: BREACH` firing while the box sat at 39%
of RAM against its 60% limit with 2.3 GB free.

overseer-policy.md invariant 17: severity is a property of the invariant
breached, not of the check that emitted it — and the tiering must be checked in
BOTH directions, because getting one half right and the other wrong is the
common shape of this bug.
"""

from __future__ import annotations

import re

import pytest

from tests.box_health_helpers import (
    BOX_HEALTH,
    classify,
    classifier_source,
    emitted_problem_lines,
    partition,
)

# Conditions where a product or the box is degraded right now, or where a unit
# cannot come back if it restarts. These MUST push.
CRITICAL = [
    "service down: vires.service",
    "service not answering HTTP within 5s: vires.service",
    "port not listening: 8980",
    "low memory: <150MB available",
    "disk critical: root >=90% used",
    "memory pressure: vires.service is stalled on reclaim against its memory cap",
    "timer job failing: morning-signal.timer (last run result=exit-code)",
    "unit cannot restart: metron-api.service has User=svc-metron, which does not resolve",
]

# A declared invariant is breached, or our ability to observe one is impaired,
# while nothing is degraded. These must NOT push, and must still be published.
WARNING = [
    "cgroup throttle: dashboard.service hit MemoryHigh 137x since the last check",
    "disk high: root >=80% used",
    "timer has not run in 2d (budget 26h): metron-refresh.timer",
    "timer enabled but not active (will not fire until reboot): box-hygiene.timer",
    "timer will never fire again: fstrim.timer",
    "service running but NOT enabled: nousergon-auth.service",
    "watchdog: throttle state dir not writable (/var/lib/box-health) — cgroup throttling is UNMONITORED",
    "watchdog: service manifest missing (/etc/box-health/manifest) — using stale fallback list",
    "watchdog: memory budget check failed to run (rc=3)",
    # alpha-engine-config-I8990: a morning-signal run on stale code is real
    # and standing, but not "degraded right now" — the `-` on its
    # ExecStartPre is a settled, ruled-on decision (episode generation must
    # never be skipped by a transient sync blip). Console-visible, not paged.
    "morning-signal stale: last sync attempt FAILED 3h ago — the service still starts on whatever was already checked out (abc1234), by design (ExecStartPre=-, see alpha-engine-config-I8990)",
    "morning-signal stale: last sync REPORTED success 1d ago landing on abc1234, but origin/main is now def5678 and no sync has run since — check whether morning-signal.timer/-pull.timer are still firing",
]

INFO = [
    # A sync state file that is missing because nothing has run YET is
    # absence of evidence, not a fault. It was `watchdog:` (warning, which
    # publishes) for a few hours on 2026-08-28; with a 10-minute cadence and
    # a 7.5-hour wait for the next morning-signal.timer elapse that is ~45
    # published lines about a healthy box. Console-visible with its age,
    # never in the channel.
    "notice: morning-signal sync state not yet recorded (/tmp/nousergon-git-sync-state-morning-signal.json) — morning-signal-sync.sh has not run since it was installed; expected until the next morning-signal.timer / -pull.timer elapse",
    # T1-8 moved warning -> info on 2026-08-21 (alpha-engine-config-I7858,
    # Brian ruling: "if i'm 4x away from the wall then i certainly no longer
    # want to be alerted of it"). It is a HEADROOM invariant, not the exit
    # trigger — E3 is `MemAvailable < 250 MB` and was 4.5x away while this
    # fired for 90 of 274 runs. Note it has no `notice: ` prefix and is
    # classified by its own arm, so the prefix is not what defines this tier.
    "memory budget: BREACH (detail in journal)",
    "notice: memory budget observation hygiene (detail in journal)",
    "notice: timer has no dead-man threshold: foo.timer — add a timers: row to budget.yaml",
]

# krepis.alerts.SEVERITY_PUSH. Duplicated here as a CONTRACT, not as config: if
# the library narrows it, this list is what has to be revisited deliberately.
PUSHES = {"error", "critical"}


@pytest.mark.parametrize("line", CRITICAL)
def test_degraded_conditions_push(line: str) -> None:
    """The direction the 2026-07-29 fix was for. Do not silence these."""
    tier = classify(line)
    assert tier in PUSHES, (
        f"{line!r} classified {tier!r}, which krepis.alerts delivers silently. "
        "This condition means a product is down or cannot restart; it must wake "
        "somebody."
    )


@pytest.mark.parametrize("line", WARNING)
def test_declared_invariant_findings_do_not_push(line: str) -> None:
    """The direction this change is for."""
    tier = classify(line)
    assert tier not in PUSHES, (
        f"{line!r} classified {tier!r}, which pushes a phone notification. "
        "Nothing is degraded when this fires; it belongs to the Overseer."
    )
    assert tier == "warning", (
        f"{line!r} classified {tier!r}; `info` is the console hygiene tier "
        "and would delay a real invariant breach by up to 24h"
    )


@pytest.mark.parametrize("line", INFO)
def test_monitoring_hygiene_is_info(line: str) -> None:
    assert classify(line) == "info"


def test_classification_is_total_over_every_emitted_problem() -> None:
    """No problem line may reach the classifier's fall-through.

    The fall-through exists as a fail-loud backstop — an unclassified line pages
    rather than going quiet — but relying on it means a tiering decision was
    never made. This asserts every line the script can emit was decided
    explicitly.
    """
    source = classifier_source()
    lines = emitted_problem_lines()
    assert len(lines) >= 20, (
        f"harvested only {len(lines)} problem strings from box_health.sh; the "
        "harvester has probably stopped matching and this test is no longer "
        "protecting anything"
    )

    prefixes = _case_prefixes(source)
    assert len(prefixes) >= 15, (
        f"parsed only {len(prefixes)} case arms out of the classifier; the arm "
        "parser has stopped matching and this test would pass vacuously"
    )

    unmatched = [
        line for line in lines if not any(line.startswith(p) for p in prefixes)
    ]
    assert not unmatched, (
        "these problem lines fall through to the default tier, so they page by "
        "accident rather than by decision — add a case arm for each:\n  "
        + "\n  ".join(unmatched)
    )


def _case_prefixes(source: str) -> list[str]:
    """The literal prefix of each case arm, excluding the `*)` default.

    Arms are written `"some prefix"*) echo tier ;;`, so the literal is exactly
    what is inside the quotes.
    """
    return re.findall(r'^\s*"([^"]+)"\*\)', source, re.MULTILINE)


def test_default_arm_pages_rather_than_silencing() -> None:
    """A check added without a tiering decision must fail LOUD.

    This is the guard-observed-failing requirement (overseer-policy.md invariant
    13) applied to the classifier itself: fed a line nobody wrote an arm for, it
    must produce a pushing tier. If someone changes the default to `warning`,
    every future un-tiered check goes silently missing and this is the only
    thing that notices.
    """
    tier = classify("some condition nobody has classified yet")
    assert tier in PUSHES, (
        f"the classifier's default arm returned {tier!r}, which is silent. An "
        "un-tiered problem must page, never vanish."
    )


def test_all_three_tiers_still_reach_a_surface() -> None:
    """Not delivered to the channel is not the same as unrecorded.

    `critical` and `warning` publish through krepis.alerts -- the argument for
    the warning tier's silence is that it still reaches SNS and the Overseer
    intake bus, so if that publish call goes away the argument goes with it.

    `info` deliberately does NOT publish there. It reaches the console's
    fleet-check surface instead, because `disable_notification` was never
    invisibility: krepis.alerts' SEVERITY_PUSH is {error, critical} and every
    other severity still LANDS IN THE CHAT, just without a phone buzz. Two
    previous fixes tuned this tier's cadence and severity; neither controlled
    its visibility, which was the actual complaint. What this test pins is that
    the tier still has exactly one delivery path and has not simply been
    dropped -- see test_box_health_hygiene_console_routing.py for the other half.
    """
    for severity in ("critical", "warning"):
        assert f"publish_problems {severity} " in BOX_HEALTH, (
            f"the {severity} tier is classified but never published"
        )
    assert "publish_problems info " not in BOX_HEALTH, (
        "the info tier publishes to krepis.alerts again. `info` is delivered "
        "silently but VISIBLY into Brian's channel; that is the defect this "
        "routing removed."
    )
    assert "emit_hygiene_envelope" in BOX_HEALTH, (
        "the info tier is classified and partitioned but reaches no surface at "
        "all -- that is suppression, not routing."
    )


def test_partition_routes_a_mixed_set_and_renders_no_phantom_bullet() -> None:
    """End-to-end over the SHIPPED partition, not a transcription of it.

    Two things are asserted together because they fail together. Each line must
    land in its own tier, AND each tier's payload must survive
    publish_problems' `mapfile -t _p <<< "$payload"` without a trailing empty
    element. `<<<` appends a newline, so a payload ending in "\\n" yields a
    phantom final element that renders as a bare " - " bullet and lands in the
    dedup key. The previous implementation got this right for free because
    `$( ... )` strips trailing newlines; building the strings in the shell does
    not, and nothing about the difference is visible until an alert fires.
    """
    confirmed = [
        "service down: vires.service",
        "memory budget: BREACH (detail in journal)",
        "notice: memory budget observation hygiene (detail in journal)",
        "port not listening: 8980",
        "watchdog: ss probe returned no output (cannot verify ports)",
    ]
    got = partition(confirmed)

    assert got["criticals"] == ["service down: vires.service", "port not listening: 8980"]
    # `watchdog: ` is the only warning left in this fixture. The T1-8 breach
    # moved to notices on 2026-08-21 (alpha-engine-config-I7858) — and its
    # presence there, WITHOUT a `notice: ` prefix, is the property worth
    # asserting: the info tier is defined by the classifier, not by a string
    # prefix, so a reader cannot infer the tier from the text alone.
    assert got["warnings"] == [
        "watchdog: ss probe returned no output (cannot verify ports)",
    ]
    assert got["notices"] == [
        "memory budget: BREACH (detail in journal)",
        "notice: memory budget observation hygiene (detail in journal)",
    ]

    for tier, entries in got.items():
        assert all(e.strip() for e in entries), (
            f"{tier} contains an empty element, which renders as a bare ' - ' "
            f"bullet and pollutes the dedup key: {entries!r}"
        )


def test_verdict_metric_counts_only_critical() -> None:
    """`is the box unhealthy` must not be answered by a bookkeeping finding."""
    calls = re.findall(r'^publish_verdict "\$\(printf[^\n]*', BOX_HEALTH, re.MULTILINE)
    assert len(calls) == 1, (
        f"expected exactly one computed publish_verdict call, found {len(calls)}"
    )
    assert "criticals" in calls[0], (
        "the verdict gauge must be computed from the critical tier only; "
        f"folding warnings in is what made it track bookkeeping. Got: {calls[0]}"
    )
    assert "warnings" not in calls[0] and "notices" not in calls[0]
