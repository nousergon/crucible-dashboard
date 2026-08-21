"""The deploy's rollback venv copies are bounded.

WHY (alpha-engine-config-I8036)
--------------------------------
`deploy-on-merge.sh`'s python-parity self-heal preserves the outgoing venv at
`$REPO_DIR/.venv-prev-<epoch>` so `_rollback_venv` can restore it after a failed
interpreter swap (the config#2835 postmortem — an earlier flow left four
services crash-looping for ~25 minutes because a failed swap did NOT restore
it). Nothing ever deleted the copy on success, so each self-heal left a
permanent ~700 MB directory and the total was bounded only by how often that
path runs.

Measured on i-09b539c844515d549, 2026-08-21: one such directory, 711 MB,
mtime 2026-03-10 — five months of rollback window for a swap whose health gate
passed the same minute.

It is not invisible: `nous-ergon-ops/.../krepis-venv/co-tenants.yaml` declares
`.venv-prev-*` as a glob with `posture: retired`, deliberately counting the
krepis 0.14.0 inside it rather than filtering it out. So this is not "an
unaccounted-for directory"; it is an accounted-for one with no retention.

WHY NOT box_hygiene.sh: its own header says it deliberately does not touch
venvs, and it is right to — it cannot know which venv a deploy is mid-swap on.
The creating site can, and the prune runs after every health gate has passed,
so the venv being backed up is not merely installed but serving traffic.

WHY A WINDOW AT ALL: `_rollback_venv` only covers failures inside the self-heal
block. A Python-level regression that passes an HTTP health check and surfaces
hours later has no automatic path back, and a `mv` is seconds where a rebuild
from requirements.txt is minutes of downtime.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "infrastructure" / "deploy-on-merge.sh"
SRC = DEPLOY.read_text()


def test_a_retention_window_is_declared():
    m = re.search(r"^VENV_PREV_RETENTION_DAYS=(\d+)$", SRC, re.MULTILINE)
    assert m, "deploy-on-merge.sh declares no VENV_PREV_RETENTION_DAYS"
    days = int(m.group(1))
    assert 1 <= days <= 30, (
        f"retention of {days}d is outside the defensible range: under a day "
        f"removes the manual rollback window the self-heal's own postmortem "
        f"argues for, over a month is the unbounded growth this fixes"
    )


def test_the_prune_is_scoped_to_the_rollback_copies():
    """`-maxdepth 1` and the `.venv-prev-*` name, both.

    Without maxdepth this walks into `.venv` itself — hundreds of thousands of
    files on every deploy — and a looser glob would reach `.venv-intraday` in a
    sibling checkout, which is LIVE (metron-intraday.service execs it directly).
    """
    prune = SRC[SRC.index("VENV_PREV_RETENTION_DAYS="):]
    assert "-maxdepth 1" in prune
    assert "-name '.venv-prev-*'" in prune
    assert "-type d" in prune


def test_the_prune_runs_after_the_health_gate():
    """Order is the safety property.

    Pruning before the gate would delete a rollback copy while the thing it
    could roll back to is still unproven — which is precisely the window the
    copy exists for.
    """
    gate = SRC.rindex('revert_to_last_good "$health_failed health check failed"')
    prune = SRC.index("VENV_PREV_RETENTION_DAYS=")
    assert prune > gate


def test_the_prune_never_aborts_the_deploy():
    """A failed `rm` is logged, not fatal.

    This runs after the deploy has already succeeded and been stamped
    last-good. Turning a disk-hygiene failure into a deploy failure would
    invert the severity — and `revert_to_last_good` is no longer armed for it.
    """
    prune = SRC[SRC.index("VENV_PREV_RETENTION_DAYS="):]
    assert "WARN could not remove" in prune
    assert "fail " not in prune


def test_the_removal_is_logged_with_its_size():
    """A silent reclaim of 700 MB is indistinguishable from a bug that ate it."""
    prune = SRC[SRC.index("VENV_PREV_RETENTION_DAYS="):]
    assert "pruning rollback venv older than" in prune
    assert "du -sh" in prune
