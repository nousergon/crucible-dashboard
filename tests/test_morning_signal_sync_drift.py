"""alpha-engine-config-I8990: detect the missing EFFECT of a morning-signal
sync that failed under `ExecStartPre=-`, never the missing event.

WHY THIS EXISTS
----------------
morning-signal.service and morning-signal-bakeoff.service invoke
morning-signal-sync.sh via a deliberately failure-tolerant `ExecStartPre=-`
(episode generation must never be skipped by a transient sync blip — that
decision stands and is NOT what this closes). The consequence: when a sync
fails past its own 5-attempt retry, systemd ignores the exit status, the
service starts on whatever code was already on disk, produces a
normal-looking signal, and exits 0. `Result=success` afterwards, so
box_health.sh's classify_timer_staleness sees a healthy unit. Nothing
anywhere said the run was stale.

The fix has two halves, both exercised here against the SHIPPED bytes
(never a re-implementation, same rule test_git_sync_lock_coverage.py and
box_health_helpers.py already follow for this file):

  1. morning-signal-sync.sh records SYNC_OK / HEAD_SHA / SYNCED_AT to a
     small state file (git_sync_state_path) on EVERY exit path, success or
     failure — exercised live, in both directions, against a real local
     git remote (test_sync_records_state_on_success /
     test_sync_records_state_on_failure_and_leaves_checkout_unchanged).
  2. box_health.sh's check_morning_signal_sync_drift reads that state and
     reports a `morning-signal stale: ` finding — classified `warning`
     (console-visible with its age, never a phone push) rather than
     `critical`, because the `-` prefix it is downstream of is itself an
     already-ruled-on, accepted risk, not an outage.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH_PATH = REPO_ROOT / "infrastructure" / "box_health.sh"
BOX_HEALTH = BOX_HEALTH_PATH.read_text()
SYNC_SCRIPT = REPO_ROOT / "infrastructure" / "morning-signal-sync.sh"
GIT_SYNC_LOCK = REPO_ROOT / "infrastructure" / "lib" / "git-sync-lock.sh"
INSTALL_BOX_HEALTH = REPO_ROOT / "infrastructure" / "install-box-health.sh"

_HAVE_FLOCK = shutil.which("flock") is not None
_HAVE_GIT = shutil.which("git") is not None


def _func_source(text: str, name: str) -> str:
    m = re.search(r"^" + re.escape(name) + r"\(\)\s*\{.*?^\}", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise AssertionError(f"{name}() not found — anchored extraction failed")
    return m.group(0)


# ── static wiring ────────────────────────────────────────────────────────

def test_git_sync_lock_defines_state_path_helper():
    src = GIT_SYNC_LOCK.read_text()
    assert "git_sync_state_path()" in src
    assert "/tmp/nousergon-git-sync-state-" in src


def test_box_health_sources_git_sync_lock_and_defines_the_drift_check():
    assert "lib/git-sync-lock.sh" in BOX_HEALTH
    assert '"$(dirname "${BASH_SOURCE[0]}")/git-sync-lock.sh"' in BOX_HEALTH
    _func_source(BOX_HEALTH, "check_morning_signal_sync_drift")  # raises if missing


def test_box_health_calls_the_drift_check():
    assert "check_morning_signal_sync_drift" in BOX_HEALTH
    # Called, not just defined: the definition line ends in `() {`, a call
    # site is the bare name on its own statement.
    call_sites = [
        line for line in BOX_HEALTH.splitlines()
        if line.strip() == "check_morning_signal_sync_drift"
    ]
    assert call_sites, (
        "check_morning_signal_sync_drift is defined but never called — a "
        "detector nothing invokes is the defect this issue is about, one "
        "layer up"
    )


def test_install_box_health_installs_git_sync_lock_flat():
    src = INSTALL_BOX_HEALTH.read_text()
    assert "/usr/local/bin/git-sync-lock.sh" in src, (
        "installed box_health.sh resolves git_sync_state_path via a flat "
        "sibling at /usr/local/bin/git-sync-lock.sh (same two-candidate "
        "pattern morning-signal-sync.sh itself uses) — install-box-health.sh "
        "must place it there or the drift check silently degrades to a "
        "watchdog: finding on every run"
    )


def test_new_finding_is_classified_warning_not_critical():
    """Routed to the console, never the phone (Brian, 2026-08-26: 'i don't
    want to be paged with box health at all if there is no issue'). A stale
    morning-signal run is real and standing, but the `-` it is downstream of
    is an already-accepted risk, not a current outage."""
    from tests.box_health_helpers import classify

    assert classify("morning-signal stale: last sync attempt FAILED 3h ago") == "warning"
    assert classify("watchdog: morning-signal sync state missing") == "warning"


# ── live functional tests: morning-signal-sync.sh writes state on BOTH
#    success and failure, against a real local git remote ──────────────────

@pytest.fixture()
def git_remote_and_checkout(tmp_path: Path):
    if not _HAVE_GIT:
        pytest.skip("git not available")
    remote = tmp_path / "remote.git"
    # `-b main` is NOT optional. Without it the branch name comes from the
    # machine's `init.defaultBranch`, which is `main` on the laptop this was
    # written on and `master` on the CI runner — so every `push origin main`
    # below failed in CI and only in CI (2026-08-28, run 33138344414). Pin the
    # name in the fixture rather than depending on the host's git config.
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    (seed / "f").write_text("a\n")
    subprocess.run(["git", "-C", str(seed), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)

    return remote, seed, checkout


def _state_path(checkout: Path) -> Path:
    base = checkout.name
    return Path(f"/tmp/nousergon-git-sync-state-{base}.json")


def _lock_path(checkout: Path) -> Path:
    base = checkout.name
    return Path(f"/tmp/nousergon-git-sync-{base}.lock")


def _read_state(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock not available on this platform")
def test_sync_records_state_on_success(git_remote_and_checkout, tmp_path, monkeypatch):
    remote, seed, checkout = git_remote_and_checkout
    state_path = _state_path(checkout)
    lock_path = _lock_path(checkout)
    for p in (state_path, lock_path):
        p.unlink(missing_ok=True)

    # Push a second commit so the sync actually has something to land on.
    (seed / "f2").write_text("b\n")
    subprocess.run(["git", "-C", str(seed), "add", "f2"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "second"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
    remote_head = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    env = {**__import__("os").environ, "AE_GIT_SYNC_LOCK_WAIT": "10"}
    r = subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(checkout)], capture_output=True, text=True, env=env, timeout=60
    )
    assert r.returncode == 0, r.stderr

    checkout_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert checkout_head == remote_head

    assert state_path.is_file(), "morning-signal-sync.sh did not write a state file on success"
    state = _read_state(state_path)
    assert state["SYNC_OK"] == "1"
    assert state["HEAD_SHA"] == remote_head
    assert state["SYNCED_AT"].isdigit()

    state_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock not available on this platform")
def test_sync_records_state_on_failure_and_leaves_checkout_unchanged(git_remote_and_checkout):
    """Demonstration 1 of the issue's closes-when: stage a sync FAILURE and
    show it is recorded — the second half (the detector firing on it) is
    covered by test_drift_check_fires_on_a_failed_sync below, extracted
    straight from box_health.sh's own shipped source."""
    remote, seed, checkout = git_remote_and_checkout
    state_path = _state_path(checkout)
    lock_path = _lock_path(checkout)
    for p in (state_path, lock_path):
        p.unlink(missing_ok=True)

    before_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    # Point origin at a nonexistent local path — `git fetch` fails
    # immediately and deterministically, no real network needed.
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "set-url", "origin", "/nonexistent/does-not-exist.git"],
        check=True,
    )

    env = {
        **__import__("os").environ,
        "AE_GIT_SYNC_LOCK_WAIT": "10",
        "AE_GIT_FETCH_RETRIES": "1",
        "AE_GIT_FETCH_SLEEP": "0",
    }
    r = subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(checkout)], capture_output=True, text=True, env=env, timeout=30
    )
    assert r.returncode != 0, "a fetch against a nonexistent remote must fail loud"

    after_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert after_head == before_head, (
        "a failed sync must leave the checkout exactly where it was — this "
        "is what lets ExecStartPre=- run last-good code"
    )

    assert state_path.is_file(), "morning-signal-sync.sh did not write a state file on failure"
    state = _read_state(state_path)
    assert state["SYNC_OK"] == "0"
    assert state["HEAD_SHA"] == before_head

    state_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)


# ── the detector itself, extracted verbatim from box_health.sh ─────────────

def _drift_check_script(checkout_dir: str, state_stale_min: int = 36 * 60, verify_grace_min: int = 26 * 60) -> str:
    human_age = _func_source(BOX_HEALTH, "human_age")
    check_fn = _func_source(BOX_HEALTH, "check_morning_signal_sync_drift")
    return "\n".join([
        "set -uo pipefail",
        GIT_SYNC_LOCK.read_text(),
        f"MORNING_SIGNAL_DRIFT_STATE_STALE_MIN={state_stale_min}",
        f"MORNING_SIGNAL_DRIFT_VERIFY_GRACE_MIN={verify_grace_min}",
        human_age,
        check_fn,
        f'MORNING_SIGNAL_CHECKOUT_DIR="{checkout_dir}"',
        "check_morning_signal_sync_drift",
    ])


def _run_drift_check(checkout_dir: str, **kwargs) -> str:
    bash = shutil.which("bash")
    r = subprocess.run(
        [bash, "-c", _drift_check_script(checkout_dir, **kwargs)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"check_morning_signal_sync_drift exited {r.returncode}: {r.stderr}"
    return r.stdout.strip()


def _write_state(checkout: Path, *, sync_ok: str, head_sha: str, age_min: int) -> Path:
    import time

    state_path = _state_path(checkout)
    synced_at = int(time.time()) - age_min * 60
    state_path.write_text(f"SYNC_OK={sync_ok}\nHEAD_SHA={head_sha}\nSYNCED_AT={synced_at}\n")
    return state_path


def test_drift_check_is_silent_when_no_checkout(tmp_path):
    out = _run_drift_check(str(tmp_path / "does-not-exist"))
    assert out == ""


def test_drift_check_reports_watchdog_when_state_file_missing(tmp_path):
    checkout = tmp_path / "morning-signal"
    checkout.mkdir()
    _state_path(checkout).unlink(missing_ok=True)
    out = _run_drift_check(str(checkout))
    assert out.startswith("watchdog: morning-signal sync state missing")


def test_drift_check_fires_on_a_failed_sync(tmp_path):
    """Demonstration 2 (paired with test_sync_records_state_on_failure_and_
    leaves_checkout_unchanged above) of the issue's closes-when: a morning-
    signal run that starts with a FAILED git sync produces an observable,
    non-silent finding."""
    checkout = tmp_path / "morning-signal"
    checkout.mkdir()
    state_path = _write_state(checkout, sync_ok="0", head_sha="deadbeef", age_min=5)
    try:
        out = _run_drift_check(str(checkout))
        assert out.startswith("morning-signal stale: last sync attempt FAILED")
        assert "deadbeef" in out
    finally:
        state_path.unlink(missing_ok=True)


def test_drift_check_is_green_on_a_healthy_recent_sync(tmp_path):
    """Demonstration: a green run must stay green — the other half of the
    issue's closes-when."""
    checkout = tmp_path / "morning-signal"
    checkout.mkdir()
    state_path = _write_state(checkout, sync_ok="1", head_sha="deadbeef", age_min=5)
    try:
        out = _run_drift_check(str(checkout))
        assert out == ""
    finally:
        state_path.unlink(missing_ok=True)


def test_drift_check_reports_watchdog_when_no_timer_has_run_in_days(tmp_path):
    checkout = tmp_path / "morning-signal"
    checkout.mkdir()
    state_path = _write_state(checkout, sync_ok="1", head_sha="deadbeef", age_min=37 * 60)
    try:
        out = _run_drift_check(str(checkout))
        assert out.startswith("watchdog: morning-signal sync state at")
        assert "old" in out
    finally:
        state_path.unlink(missing_ok=True)


def test_drift_check_ignores_normal_same_day_lag_on_a_reported_success(tmp_path):
    """A successful sync whose HEAD trails origin/main by a few hours is
    NORMAL — morning-signal runs on a once-daily schedule, so same-day lag
    is not yet a defect. Only past MORNING_SIGNAL_DRIFT_VERIFY_GRACE_MIN
    does the independent ls-remote comparison kick in, and it needs a
    reachable origin — a checkout with no remote at all degrades to
    'could not reach origin', never to a false 'no drift'."""
    checkout = tmp_path / "morning-signal"
    checkout.mkdir()
    state_path = _write_state(checkout, sync_ok="1", head_sha="deadbeef", age_min=60)
    try:
        out = _run_drift_check(str(checkout))
        assert out == "", "same-day lag on a reported-success sync must not fire"
    finally:
        state_path.unlink(missing_ok=True)
