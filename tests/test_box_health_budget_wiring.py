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
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()


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


def test_notice_lines_do_not_page_and_alerts_do():
    """The severity split itself.

    krepis.alerts pushes a phone notification for error/critical only; warning
    and info are silent in-channel. Before 2026-07-29 every box-health problem
    published at `warning`, so a service being DOWN pushed exactly as loudly as
    a missing budget row: not at all. Both halves are asserted because getting
    one right and the other wrong is the likely regression.
    """
    assert re.search(r"publish_problems error\s+\d+", BOX_HEALTH), (
        "alert-class problems must publish at `error` so they actually push"
    )
    assert re.search(r"publish_problems info\s+\d+", BOX_HEALTH), (
        "notice-class problems must publish at `info` (silent in-channel)"
    )
    assert "grep -v '^notice: '" in BOX_HEALTH and "grep '^notice: '" in BOX_HEALTH, (
        "the two tiers must be partitioned by the `notice: ` prefix"
    )


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
