"""Tests for boot-pull.sh's morning-signal sync pass (alpha-engine-config-I6656).

Until 2026-08-08, eleven files that ran the podcast in production existed on
this box and in no repository. They are now codified in nous-ergon-ops
(nous-ergon-ops-PR522); this pass is what actually carries them to the box,
which is the half that makes codification worth anything — a merge nothing
delivers changes nothing.

Mirrors `test_boot_pull_metron_intraday_sync.py` in shape, and asserts the
three things about this block that are easy to get wrong and impossible to
notice: where it sources from, that drop-ins travel with the unit, and that
the operator script travels with it too.
"""
from __future__ import annotations

from pathlib import Path

_BOOT_PULL = Path(__file__).parent.parent / "infrastructure" / "boot-pull.sh"


def _source() -> str:
    return _BOOT_PULL.read_text()


def test_source_dir_is_nous_ergon_ops_not_the_public_repo():
    """The public `morning-signal` repo carries
    `infrastructure/morning-signal-watchdog.{service,timer}` — and they are
    OSS EXAMPLES: `User=podcast`, `/home/podcast/...`, "adjust to match your
    install". Syncing from those would install a unit for a user that does
    not exist on this box, and it would do it silently.

    Their hashes differ from the installed units, so the mistake also reads
    exactly like drift. It nearly became a false finding on 2026-08-08.
    """
    src = _source()
    assert (
        'MORNING_SIGNAL_SRC="/home/ec2-user/nous-ergon-ops/morning-signal/infrastructure"'
        in src
    ), "morning-signal units must be sourced from nous-ergon-ops, the operated-system assembly"
    assert "/home/ec2-user/morning-signal/infrastructure" not in src, (
        "must NOT source units from the public morning-signal checkout — those "
        "files are self-hoster examples, not this box's deployed units"
    )


def test_nous_ergon_ops_is_in_the_repos_array():
    """The sync block reads a checkout the pull loop has to keep current.
    Without the REPOS entry it would converge the box toward whatever was on
    disk the day someone cloned it."""
    src = _source()
    # Split on a `)` at the start of a line: the array's comments contain
    # parenthesised issue references, so splitting on a bare `)` truncates
    # the block mid-comment and silently under-asserts.
    repos_block = src.split("REPOS=(", 1)[1].split("\n)", 1)[0]
    assert "/home/ec2-user/nous-ergon-ops" in repos_block


def test_the_checkout_is_sparse():
    """A full clone is 76M of history plus the entire policy library, on a
    box with 1GB of RAM, to deliver eleven files. The clone command lives in
    the block's header so the next person to provision a box has it."""
    src = _source()
    assert "sparse-checkout set morning-signal" in src
    assert "--filter=blob:none --sparse" in src


def test_sync_is_scoped_to_exact_basenames():
    """Never a directory-wide glob: the source directory must not be able to
    grow a new unit onto this box because somebody added a file to it."""
    src = _source()
    for unit in (
        "morning-signal.service",
        "morning-signal.timer",
        "morning-signal-bakeoff.service",
        "morning-signal-bakeoff.timer",
        "morning-signal-watchdog.service",
        "morning-signal-watchdog.timer",
    ):
        assert unit in src, f"{unit} missing from the sync loop"


def test_dropins_are_synced_with_the_unit():
    """The four drop-ins are load-bearing — ordering against daily-news, the
    memory cap, the TimeoutStartSec raise that a 2026-08-08 episode died
    without, and the router-edge env. A unit delivered without its drop-ins
    is a different unit.
    """
    src = _source()
    assert 'DROPIN_SRC="$MORNING_SIGNAL_SRC/systemd/morning-signal.service.d"' in src
    assert 'DROPIN_DST="/etc/systemd/system/morning-signal.service.d"' in src


def test_a_dropin_removed_from_the_repo_is_removed_from_the_box():
    """Copy-only convergence cannot delete. Without this, a retired drop-in
    keeps applying forever with nothing in any repo pointing at it — which is
    the same class of invisible state this whole issue is about."""
    src = _source()
    assert "REMOVED — gone from repo" in src, (
        "the drop-in sync must remove installed .conf files that no longer "
        "exist in the repo"
    )


def test_the_operator_script_travels_with_the_unit():
    """morning-signal-recover.sh re-runs what morning-signal.service runs. On
    2026-08-08 the unit gained three router env vars via a drop-in and the
    script did not, so the recovery run resolved against a different
    execution context than the run it was recovering."""
    src = _source()
    assert 'MS_RECOVER_SRC="$MORNING_SIGNAL_SRC/bin/morning-signal-recover.sh"' in src
    assert "install -m 0755" in src, "the operator script must land executable"


def test_timers_are_enable_reconciled_every_run():
    """Install-once is not enough: a manual `systemctl disable` or a lost
    timers.target.wants/ symlink never self-heals, and the failure mode is a
    podcast that silently stops airing."""
    src = _source()
    block = src.split("MORNING_SIGNAL_SRC=", 1)[1]
    assert "for timer in morning-signal.timer morning-signal-bakeoff.timer morning-signal-watchdog.timer" in block
    assert "enable reconciled $timer" in block
