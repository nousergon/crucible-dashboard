"""Tests for boot-pull.sh's metron-intraday sync pass (config#1768 Phase 1).

metron-intraday moved off ae-trading onto this box (2026-07-21) — see
crucible-executor's boot-pull.sh (the trading side of the same move) and
nousergon-data's infrastructure/systemd/ (canonical unit-file source, this
box already clones that repo as alpha-engine-data). These tests pin that
boot-pull.sh actually picks up the unit, scoped ONLY to metron-intraday
(not the whole shared source dir, which also ships daily-news +
systemd-unit-drift-check via a separate, pre-existing install path) and
self-heals the enable state every boot rather than install-once.
"""
from __future__ import annotations

from pathlib import Path

_BOOT_PULL = Path(__file__).parent.parent / "infrastructure" / "boot-pull.sh"


def _source() -> str:
    return _BOOT_PULL.read_text()


def test_boot_pull_exists():
    assert _BOOT_PULL.exists(), f"boot-pull.sh missing at {_BOOT_PULL}"


def test_metron_intraday_source_dir_is_nousergon_data():
    """Unit files stay canonical in nousergon-data's infrastructure/systemd/
    — this repo must not carry a duplicate copy of metron-intraday's unit
    files, only reference nousergon-data's (cloned here as alpha-engine-data,
    per this box's own REPOS array)."""
    src = _source()
    assert (
        'METRON_INTRADAY_SRC="/home/ec2-user/alpha-engine-data/infrastructure/systemd"'
        in src
    ), (
        "boot-pull.sh must source metron-intraday's unit files from "
        "nousergon-data's infrastructure/systemd/ (cloned as "
        "alpha-engine-data on this box), not a local copy."
    )


def test_metron_intraday_sync_scoped_to_exact_basenames():
    """The sync must be scoped to the two exact metron-intraday basenames —
    never a directory-wide glob, since the source dir also ships
    daily-news.{service,timer} (separate install-daily-news.sh +
    deploy-daily-news-units.yml merge-time-push path already covering this
    box) and systemd-unit-drift-check.{service,timer} (also already
    installed via install-daily-news.sh). A directory-wide glob here would
    double-install/-restart daily-news via two independent mechanisms."""
    src = _source()
    assert "for unit in metron-intraday.service metron-intraday.timer" in src, (
        "the metron-intraday sync loop must iterate the two exact unit "
        "basenames, not a wildcard glob over the shared nousergon-data "
        "systemd source dir."
    )


def test_metron_intraday_timer_enable_reconciled_every_boot():
    """The timer must be enable-reconciled on every boot-pull run (not just
    on first install) — mirrors ae-trading's sync_systemd_units_from()
    self-healing pattern (config#2352 / 2026-04-21 SNDK EOD incident class).
    This is a brand-new unit family for this box, so a manual
    `systemctl disable` or a lost timers.target.wants/ symlink would
    otherwise never self-heal."""
    src = _source()
    assert "systemctl enable --now metron-intraday.timer" in src, (
        "boot-pull.sh must `systemctl enable --now metron-intraday.timer` "
        "every run (idempotent on an already-enabled timer, self-healing "
        "on a disabled one)."
    )


def test_metron_intraday_sync_runs_before_streamlit_restart_gate():
    """The metron-intraday sync block must be wired in before the
    CONFIGS_CHANGED streamlit-restart gate at the end of the script (i.e.
    actually reachable code, not appended after the script's terminal
    exit/report block)."""
    src = _source()
    metron_pos = src.index("METRON_INTRADAY_SRC=")
    configs_changed_pos = src.index('if [ "$CONFIGS_CHANGED" -eq 1 ]')
    assert metron_pos < configs_changed_pos, (
        "metron-intraday sync block must run before the final "
        "CONFIGS_CHANGED-gated streamlit restart section."
    )
