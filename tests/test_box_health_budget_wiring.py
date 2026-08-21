"""box_health.sh must actually INVOKE the memory budget check.

WHY
---
`check_memory_budget.py`'s docstring says `--installed` "is the on-box mode, run
by box_health.sh". Verified 2026-07-28: it was not. Nothing on the box or in CI
invoked `--installed` -- only `--declared`, from the installer, which checks
budget.yaml against ITSELF and can never see the box.

So every `--installed` check -- cap drift, uncapped service, and the
censored/stale/orphan observation checks -- was written, tested, and never
executed. The docstring asserting the integration is what made it look wired.

Source-text assertions, deliberately, matching test_boot_pull_failure_reporting:
the call site lives in a bash script that runs as root on an EC2 box. Executing
it in CI is not meaningful; what these pin is the CONTRACT.
"""

import re

from tests.box_health_helpers import BOX_HEALTH, classify


def test_box_health_invokes_the_installed_budget_check():
    """The integration the docstring claimed. Without this line, nothing runs it."""
    assert "--installed" in BOX_HEALTH, (
        "box_health.sh does not invoke check_memory_budget.py --installed. "
        "Every --installed check is then dead code that runs nowhere."
    )
    assert "BUDGET_CHECK" in BOX_HEALTH


def test_budget_problem_strings_are_static():
    """LOAD-BEARING: box_health confirms problems by EXACT line intersection.

    snapshot_problems() is sampled repeatedly and only lines present in every
    sample are alerted. The budget check's own messages carry live byte counts
    ("holds 185 MB (1.7x)") which move between samples, so emitting them
    verbatim would produce a problem that can NEVER confirm -- a guard that
    looks wired and silently never fires.

    Every emitted line must therefore contain no digits-with-units.
    """
    emitted = re.findall(r'echo "(memory budget:[^"]*)"', BOX_HEALTH)
    emitted += re.findall(r'echo "(notice: memory budget[^"]*)"', BOX_HEALTH)
    assert emitted, "expected at least one static `memory budget: ...` problem line"
    for line in emitted:
        assert not re.search(r"\d+\s*(MB|MiB|GB|%|x)", line), (
            f"problem string {line!r} embeds a live measurement; it will never "
            "survive the confirm-on-retry intersection"
        )
    # The detail still has to reach the operator -- just via the journal.
    assert "memory budget detail" in BOX_HEALTH


def test_budget_exit_codes_are_tiered_by_severity():
    """rc=1 and rc=2 are different events and must not produce one alert.

    The checker separates an INVARIANT BREACH (the box is over budget) from
    OBSERVATION HYGIENE (the box is fine; something is degrading our ability to
    measure it). If box_health.sh collapses them back into one line, the page
    rate tracks bookkeeping rather than health -- which is the condition this
    split exists to end, and which is invisible unless something asserts the
    branches stay distinct.
    """
    assert re.search(r'1\)\s*echo "memory budget: BREACH', BOX_HEALTH), (
        "rc=1 must emit an alert-class line"
    )
    assert re.search(r'2\)\s*echo "notice: memory budget', BOX_HEALTH), (
        "rc=2 must emit a notice-class line -- an un-prefixed line pages"
    )
    # rc=3 (and anything unexpected) is a watchdog malfunction, not a verdict.
    assert "memory budget check failed to run" in BOX_HEALTH


def test_budget_breach_is_console_only_not_channelled():
    """A T1-8 breach reaches the CONSOLE and no longer reaches the channel.

    SUPERSEDES the 2026-07-29 "delegated, not discarded" shape this test used to
    assert, on Brian's 2026-08-21 ruling (alpha-engine-config-I7858): "if i'm 4x
    away from the wall then i certainly no longer want to be alerted of it."

    The old shape rested on two claims that did not survive measurement.

    ONE: that `warning` was quiet. It is not — krepis.alerts passes
    `disable_notification=True`, which suppresses the phone push and NOT the
    message, so every breach still landed in the chat. 90 of 274 watchdog runs
    over fourteen days, for one standing condition with an open ruling on it
    (alpha-engine-config-I7804).

    TWO: that the publish was a DELEGATION to the Overseer intake bus. Measured
    2026-08-20: all four `alpha-engine-alert-drain-*` schedules are DISABLED
    under the 2026-08-07 automation pause (alpha-engine-config-I6984). The
    delegated consumer has not been running. A publish to a paused drain is not
    delegation, it is a message to Brian with an extra step.

    THE FINDING IS NOT SILENCED, and this is the assertion that matters:
    `emit_hygiene_envelope` renders the info tier on the console on EVERY run
    including clean ones, with each finding's age. principles.md §7 — a
    component emitting nothing is unobserved, not healthy. A standing condition
    belongs on a board that shows how long it has been true, not in a stream
    that re-announces it.

    WHAT IS STILL LOUD is asserted in test_the_real_wall_is_still_critical
    below. Nothing indicating the box is actually running out of memory moved.
    """
    assert classify("memory budget: BREACH (detail in journal)") == "info", (
        "a T1-8 headroom breach is a console finding, not a channel message"
    )
    assert "emit_hygiene_envelope" in BOX_HEALTH, (
        "the info tier must still reach the console — silencing without the "
        "console publish would be the 2026-07-29 defect in reverse: a real "
        "finding reaching nobody"
    )


def test_the_real_wall_is_still_critical():
    """The conditions that mean the box IS running out of memory still page.

    Written as its own test because the change above is the one a future reader
    will summarise as "memory alerts were turned off". T1-8 is a declared
    headroom bound; these are the box actually failing.
    """
    assert classify("low memory: <250MB available") == "critical"
    assert classify(
        "memory pressure: vires.service is stalled on reclaim against its memory cap"
    ) == "critical"


def test_missing_check_is_reported_not_skipped():
    """A check that cannot run is a watchdog malfunction, not a pass.

    Same class as the df-probe guard: silence here would mean the budget check
    disappearing from the box reads exactly like the budget being healthy.
    """
    assert "watchdog: memory budget check missing" in BOX_HEALTH


def test_check_is_invoked_with_the_venv_interpreter():
    """The script needs PyYAML, which the system python3 on this box lacks."""
    assert re.search(r'"\$VENV_PY"\s+"\$BUDGET_CHECK"', BOX_HEALTH), (
        "budget check must run under the venv interpreter -- system python3 "
        "has no PyYAML and the check exits 3"
    )


def test_budget_exit_code_is_captured_not_masked():
    """`local x=$(cmd)` returns local's status, not the command's.

    The severity tiering is driven entirely by the checker's exit code, so
    capturing it wrongly would silently route every outcome to one branch. The
    declaration must be separate from the assignment.
    """
    assert re.search(r"local budget_out budget_rc", BOX_HEALTH), (
        "declare before assigning -- `local budget_out=$(...)` masks the "
        "command's exit status behind local's own"
    )
    assert re.search(r"budget_rc=\$\?", BOX_HEALTH)


# ── the console rendering path (config-I5863) ─────────────────────────────

def test_box_health_publishes_the_headroom_console_row():
    """observability-policy.md §8.1: the signal §3.3 requires must be RENDERED.

    Without this invocation, --emit-check is dead code and per-service headroom
    reaches no surface at all -- which is the state that let dashboard.service
    sit at 98.5% of its soft cap on 2026-07-31 with nothing on the console
    saying so.
    """
    assert "--emit-check" in BOX_HEALTH, (
        "box_health.sh does not invoke check_memory_budget.py --emit-check. "
        "The console row is then never published and the check renders as "
        "`unreadable` forever."
    )


def test_headroom_publish_runs_before_the_baseline_is_rewritten():
    """LOAD-BEARING ORDERING.

    throttle_baseline_write() advances the counters the delta is measured
    against. Publishing after it would make every published delta zero, and a
    zero delta is exactly what a quiet box looks like -- the console would
    report "nothing throttling" on the tick that throttled 1020 times.
    """
    emit_at = BOX_HEALTH.index("--emit-check")
    trap_at = BOX_HEALTH.index("trap throttle_baseline_write EXIT")
    assert emit_at < trap_at, (
        "the headroom publish must run before the EXIT trap that re-baselines "
        "the throttle counters"
    )


def test_headroom_publish_is_outside_snapshot_problems():
    """snapshot_problems is sampled up to RETRY_ATTEMPTS times for confirmation.

    Publishing from inside it would write the envelope four times per tick AND
    tie the console row to the confirmation intersection, so a condition that
    self-healed within the window would leave the console showing nothing --
    the opposite of what the surface is for.
    """
    body = BOX_HEALTH[BOX_HEALTH.index("snapshot_problems() {"):
                      BOX_HEALTH.index("# Gauges flow on every tick")]
    assert "--emit-check" not in body, (
        "the headroom publish belongs at run scope, not inside the sampled "
        "snapshot function"
    )


def test_a_budget_verdict_is_not_reported_as_a_publish_failure():
    """--emit-check implies --installed, so it exits 1 on a breach and 2 on a
    hygiene finding. Both are normal verdicts already reported by the snapshot
    path; treating them as publish failures would print an error line on every
    tick the box is merely tight. Only rc=3 -- could not run -- is news.
    """
    assert re.search(r'emit_check_rc" -eq 3', BOX_HEALTH), (
        "only rc=3 (check could not run) may be reported from the publish path"
    )
