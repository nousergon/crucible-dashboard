"""Every timer this repo installs must declare a dead-man threshold.

WHY
---
`box_health.sh` classifies a timer with no `timers:` row in `budget.yaml` as::

    notice: timer has no dead-man threshold: <name> - add a timers: row to budget.yaml

That finding has now been raised, fixed by hand, and raised again for a
DIFFERENT timer at least four times:

  * 2026-07-29  `metron-intraday.timer`   installed with no row - and because the
                                          no-budget branch returned early, the
                                          watchdog reported the missing row while
                                          the job was failing 48 of 48 runs.
  * 2026-08-08  three dashboard-box timers at once (alpha-engine-config-I6657).
  * 2026-08-20  `emit-service-memory.timer`, installed by
                `install-cloudwatch-agent-config.sh` the same day the T1-8
                working-set series it feeds was adopted into policy.

Each instance was closed by adding the row. None of them closed the CLASS,
because nothing structurally links "an installer enables a timer" to "budget.yaml
declares how long that timer may go quiet". The registration is hand-maintained,
so the next timer re-opens the finding, and the operator sees the same notice
again with a new name in it.

`shared-application-host-policy.md` T1-6 already states the rule this test
enforces - the watchdog's coverage "must be derived from the installed units,
not hand-listed". The service half of `budget.yaml` has a live coverage
self-check on the box (`watchdog: unmonitored enabled service(s)`); the timer
half had only an advisory notice, which is a report, not an enforcement.

This test moves the finding from the box's notice stream to CI, where it blocks
the PR that would have introduced it. A timer arrives with its threshold or it
does not arrive.

Source-text assertions, matching `test_box_health_budget_wiring.py`: the
installers run as root on an EC2 box and executing them in CI is not
meaningful. What is pinned here is the CONTRACT between two files in this repo.

SCOPE, stated because it bounds the guarantee: this covers timers whose unit
file or `systemctl enable` lives in THIS repo. Timers installed onto the shared
box by metron, nous-ergon-ops or the-cyphering cannot be checked from here.

THAT SCOPE IS NO LONGER THE END OF THE STORY (alpha-engine-config-I8034). It
held right up until 2026-08-21, when nous-ergon-ops-PR809 armed
`litellm-config-reconcile.timer` and `llm-capability-probe.timer` and the box
raised the finding for the fifth and sixth time, hours later, exactly as this
docstring predicted it would. A guard that correctly documents the case it
cannot catch is still a guard that does not catch it.

The structural half now lives in the unit file: a timer may declare
`X-DeadManStaleness=` under its own `[Unit]` section, and
`generate-box-manifest.py` merges those with `budget.yaml::timers` when it
renders `TIMER_MAX_STALENESS`. So a foreign repo's timer carries its own
threshold instead of requiring a second edit here, and each installer repo can
enforce the key against its OWN unit files - a check each of them CAN run.
`nous-ergon-ops/tests/test_every_timer_declares_a_deadman.py` is the first.

This test is unchanged and stays: budget.yaml is still the curated surface, it
still wins a disagreement, and a timer THIS repo installs still takes a row
here rather than relying on the key.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INFRA = REPO / "infrastructure"
BUDGET = INFRA / "systemd" / "resource-limits" / "budget.yaml"

# `- unit: foo.timer` under the `timers:` block. The service block uses the same
# key, so the `.timer` suffix is what selects timer rows - and it is also what
# makes a row for a *.service impossible to mistake for one.
_DECLARED_RE = re.compile(r"^\s*-\s*unit:\s*(\S+\.timer)\s*$", re.MULTILINE)

# `systemctl enable foo.timer` / `systemctl enable --now foo.timer`, with an
# optional sudo and optional leading whitespace or `if `.
_ENABLE_RE = re.compile(r"systemctl\s+enable(?:\s+--now)?\s+(\S+\.timer)")


def _declared_timers() -> set[str]:
    return set(_DECLARED_RE.findall(BUDGET.read_text()))


def _timer_unit_files() -> set[str]:
    return {p.name for p in INFRA.rglob("*.timer")}


def _installer_enabled_timers() -> set[str]:
    found: set[str] = set()
    for sh in INFRA.rglob("*.sh"):
        found.update(_ENABLE_RE.findall(sh.read_text()))
    return found


def _fail_message(missing: set[str], how: str) -> str:
    names = ", ".join(sorted(missing))
    return (
        f"{how} but has no dead-man threshold in budget.yaml: {names}.\n"
        f"Add a `- unit: <name>` row with `max_staleness:` and a `note:` under "
        f"`timers:` in {BUDGET.relative_to(REPO)}.\n"
        "Without it, box_health.sh can still see the timer stop firing and still "
        "see it fail, but CANNOT see it fire on a mis-edited OnCalendar - and it "
        "raises a `notice:` on the box on every run until the row exists."
    )


def test_every_timer_unit_file_in_this_repo_is_declared():
    """A unit file shipped here is a timer we intend to run. It needs a budget."""
    missing = _timer_unit_files() - _declared_timers()
    assert not missing, _fail_message(missing, "This repo ships a timer unit file")


def test_every_timer_an_installer_enables_is_declared():
    """Covers timers enabled here whose unit file is written by another repo.

    `boot-pull.sh` enabling `metron-intraday.timer` is the live example: the
    unit comes from metron, the decision to run it on this box is made here, so
    the declaration obligation is here too.
    """
    missing = _installer_enabled_timers() - _declared_timers()
    assert not missing, _fail_message(missing, "An installer in this repo enables a timer")


def test_the_notice_string_box_health_emits_still_names_this_file():
    """If the notice's wording drifts, this test's docstring stops being true.

    Pinned because the whole argument above rests on the on-box finding and the
    CI guard describing the SAME contract. A rename that touched only one of
    them would leave an operator reading a notice that points at a file with no
    guard, or a guard enforcing a rule nothing reports.
    """
    box_health = (INFRA / "box_health.sh").read_text()
    assert "add a timers: row to budget.yaml" in box_health, (
        "box_health.sh no longer emits the dead-man registration notice this "
        "test mirrors. Re-derive the contract before changing either side."
    )
