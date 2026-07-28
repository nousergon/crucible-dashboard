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


def test_budget_problem_string_is_static():
    """LOAD-BEARING: box_health confirms problems by EXACT line intersection.

    snapshot_problems() is sampled repeatedly and only lines present in every
    sample are alerted. The budget check's own messages carry live byte counts
    ("holds 185 MB (1.7x)") which move between samples, so emitting them
    verbatim would produce a problem that can NEVER confirm -- a guard that
    looks wired and silently never fires.

    The emitted line must therefore contain no digits-with-units.
    """
    m = re.search(r'^\s*echo "(memory budget:[^"]*)"', BOX_HEALTH, re.M)
    assert m, "expected a static `memory budget: ...` problem line"
    emitted = m.group(1)
    assert not re.search(r"\d+\s*(MB|MiB|GB|%|x)", emitted), (
        f"problem string {emitted!r} embeds a live measurement; it will never "
        "survive the confirm-on-retry intersection"
    )
    # The detail still has to reach the operator -- just via the journal.
    assert "memory budget detail" in BOX_HEALTH


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
        "has no PyYAML and the check exits 2"
    )
