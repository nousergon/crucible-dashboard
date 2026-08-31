"""Structural guard: every git write on a shared on-box checkout is flocked.

alpha-engine-config incident 2026-08-27 20:07 UTC (~/metron sibling
checkout, same class): two unsynchronised git writers collided on
`refs/remotes/origin/main` — `error: cannot lock ref
'refs/remotes/origin/main': is at 95cd989 but expected 0f2a6b8` — before
the deploy script even started, so its own failure trap never fired and
the commit sat undeployed for five hours.

This repo owns TWO shared on-box checkouts with the same defect class,
verified against origin/main:
  - /home/ec2-user/alpha-engine-dashboard: deploy.yml (SSM, on merge),
    boot-pull.sh (daily timer), substrate_health_check_daily.sh (Mon-Fri
    22:30 UTC health check) — three unsynchronised writers, no lock.
  - /home/ec2-user/morning-signal: morning-signal-pull.service,
    morning-signal.service, morning-signal-recover.sh — three
    unsynchronised writers, no lock (a fourth, morning-signal-bakeoff.service,
    shared the defect at the time and is now RETIRED — alpha-engine-config-
    I9457, morning-signal-I165).

This walks every shell script under infrastructure/ (plus every systemd
unit and .github/workflows/deploy.yml, the other two places on-box git
writers live in this repo) and fails if it finds a git command that WRITES
repository/ref state (fetch / pull / reset --hard / checkout -f / merge)
that is not itself flock-guarded.

Demonstrated failing pre-fix: reverting
infrastructure/substrate_health_check_daily.sh's
`flock -w "$GIT_SYNC_LOCK_WAIT" "$GIT_SYNC_LOCK" git pull --ff-only origin
main` back to a bare `git pull --ff-only origin main` makes
test_every_git_write_in_infrastructure_is_flocked fail on that exact line
(confirmed by hand while writing this test — see the PR body for the
transcript). Same for boot-pull.sh's per-repo fetch/reset and
morning-signal-sync.sh's fetch/reset when un-flocked.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_INFRA_DIR = _REPO_ROOT / "infrastructure"
_SYSTEMD_DIR = _INFRA_DIR / "systemd"
_DEPLOY_YML = _REPO_ROOT / ".github" / "workflows" / "deploy.yml"

# Matches a git subcommand that WRITES repository/ref state. Read-only
# commands (log, show, rev-parse, status, diff, ls-remote, cat-file -e,
# config --get) are deliberately excluded — this guard is about
# serializing writers, not about forbidding reads. `git cat-file -e` is a
# read (object-existence check) even though it looks write-adjacent.
_GIT_WRITE_RE = re.compile(
    r"\bgit\b[^|;&()]*\b(pull|fetch|merge|reset\s+--hard|checkout\s+-f)\b"
)


def _is_comment_line(line: str) -> bool:
    return line.strip().startswith("#")


def _is_non_executable_line(line: str) -> bool:
    """A comment, or a line whose FIRST token is echo/printf — a git command
    named inside a help/error message string is not an executed write."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    first_token = stripped.split(None, 1)[0] if stripped else ""
    return first_token in ("echo", "printf")


# Real, deliberate exceptions: a git write that is NOT against a shared
# on-box checkout, so it carries no cross-writer collision risk and is out
# of this incident's scope. Each entry names the file, the line's git
# subcommand for a quick eyeball match, and WHY it is safe — checked below
# to still exist so a stale entry silently widening the guard gets caught.
_NOT_A_SHARED_CHECKOUT = {
    ("spot_backtest.sh", "fetch"): (
        "operates on $REPO_ROOT, which defaults to wherever this script "
        "itself is checked out (the operator's laptop or a CI runner) — a "
        "pre-launch preflight comparing local HEAD to origin/$BRANCH before "
        "spinning up a spot instance, not a write against any shared on-box "
        "checkout."
    ),
    ("spot_backtest.sh", "merge"): (
        "same $REPO_ROOT preflight as the fetch above — merge-base "
        "--is-ancestor is a read, but the regex's `merge` alternative "
        "matches the `merge` in `merge-base` too; documented here for the "
        "same reason."
    ),
}


def _iter_shell_scripts():
    # test_*.sh harnesses stub out git/sudo/flock themselves (see
    # test_deploy_auto_revert.sh) rather than performing real writes —
    # scanning them for the literal string "git ... reset --hard" inside a
    # `fail "..."` message string is exactly the false-positive class
    # _is_non_executable_line exists to dodge for echo, and test files
    # accumulate too many of these to whitelist line-by-line.
    for path in sorted(_INFRA_DIR.rglob("*.sh")):
        if path.name.startswith("test_"):
            continue
        yield path


def _iter_systemd_units():
    yield from sorted(_SYSTEMD_DIR.glob("*.service"))


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued shell lines into one logical line, keyed by
    the line number the continuation STARTED on."""
    out: list[tuple[int, str]] = []
    pending_start: int | None = None
    pending_text = ""
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if pending_start is None:
            pending_start = lineno
            pending_text = raw_line
        else:
            pending_text += " " + raw_line.strip()
        if raw_line.rstrip().endswith("\\"):
            pending_text = pending_text.rstrip()[:-1]
            continue
        out.append((pending_start, pending_text))
        pending_start = None
        pending_text = ""
    if pending_start is not None:
        out.append((pending_start, pending_text))
    return out


def _flock_violations_in_shell(path: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    in_flock_block = False
    for lineno, line in _logical_lines(path.read_text()):
        if _is_non_executable_line(line):
            continue

        stripped = line.rstrip()

        # Multi-line `flock ... bash -c '...'` blocks (boot-pull.sh's
        # per-repo revert loop, morning-signal-sync.sh's body): every line
        # between the opening `bash -c '` and the closing bare `'` is
        # inside the lock.
        if in_flock_block:
            if stripped.lstrip().startswith("'"):
                in_flock_block = False
        opens_block = "flock" in line and stripped.endswith("bash -c '")
        if opens_block:
            in_flock_block = True

        match = _GIT_WRITE_RE.search(line)
        if not match:
            continue
        guarded = "flock" in line or in_flock_block
        if guarded:
            continue
        subcmd = match.group(1).split()[0]
        if (path.name, subcmd) in _NOT_A_SHARED_CHECKOUT:
            continue
        violations.append((lineno, line.strip()))

    return violations


def _flock_violations_in_systemd(path: Path) -> list[tuple[int, str]]:
    """A systemd unit's ExecStart*/ExecStop* lines run each as its OWN
    process — 'flock appears somewhere in the file' is not sufficient,
    the SAME line invoking the git write must itself route through
    flock (directly, or via a script that flocks internally, in which
    case the git write is not literally present in the unit at all)."""
    violations: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("#") or not line:
            continue
        if not (line.startswith("ExecStart") or line.startswith("ExecStop")):
            continue
        if _GIT_WRITE_RE.search(line) and "flock" not in line:
            violations.append((lineno, line))
    return violations


def _flock_violations_in_deploy_yml(path: Path) -> list[tuple[int, str]]:
    """deploy.yml embeds the git write inside a CMD_BODY shell string sent
    over SSM, not as a directly-executed line — check that string for a
    git write not immediately preceded/wrapped by `flock` on the same
    logical CMD_BODY assignment."""
    violations: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        if "CMD_BODY=" not in raw_line:
            continue
        if _GIT_WRITE_RE.search(raw_line) and "flock" not in raw_line:
            violations.append((lineno, raw_line.strip()[:200]))
    return violations


def test_not_a_shared_checkout_exemptions_still_match():
    """A stale exemption (the named line no longer matches, or was fixed
    to be flocked) would silently widen the guard the next time that
    (file, subcommand) pair is reused for a real shared-checkout write."""
    for (fname, subcmd) in _NOT_A_SHARED_CHECKOUT:
        path = _INFRA_DIR / fname
        assert path.is_file(), (
            f"_NOT_A_SHARED_CHECKOUT names {fname}, which no longer exists"
        )
        found = any(
            _GIT_WRITE_RE.search(line) and _GIT_WRITE_RE.search(line).group(1).split()[0] == subcmd
            for _, line in _logical_lines(path.read_text())
            if not _is_non_executable_line(line)
        )
        assert found, (
            f"_NOT_A_SHARED_CHECKOUT names ({fname!r}, {subcmd!r}) but no "
            f"matching git write was found — remove the stale entry"
        )


def test_every_git_write_in_infrastructure_is_flocked():
    all_violations: dict[str, list[tuple[int, str]]] = {}

    for path in _iter_shell_scripts():
        violations = _flock_violations_in_shell(path)
        if violations:
            all_violations[str(path.relative_to(_REPO_ROOT))] = violations

    for path in _iter_systemd_units():
        violations = _flock_violations_in_systemd(path)
        if violations:
            all_violations[str(path.relative_to(_REPO_ROOT))] = violations

    if _DEPLOY_YML.exists():
        violations = _flock_violations_in_deploy_yml(_DEPLOY_YML)
        if violations:
            all_violations[str(_DEPLOY_YML.relative_to(_REPO_ROOT))] = violations

    assert not all_violations, (
        "unflocked git write(s) found on a shared on-box checkout — every "
        "git pull/fetch/reset --hard/checkout -f/merge against "
        "alpha-engine-dashboard or morning-signal must run under that "
        "checkout's $GIT_SYNC_LOCK (infrastructure/lib/git-sync-lock.sh), "
        "or a concurrent writer can lose a ref compare-and-swap race and "
        "leave the box undeployed (alpha-engine-config incident "
        f"2026-08-27 20:07 UTC). Violations: {all_violations}"
    )


def test_git_sync_lock_shared_module_defines_the_canonical_helper():
    """infrastructure/lib/git-sync-lock.sh must exist and define both the
    wait constant and the git_sync_lock_path() function every consumer
    sources rather than hardcoding a second, non-cooperating literal."""
    shared = _INFRA_DIR / "lib" / "git-sync-lock.sh"
    assert shared.is_file(), "infrastructure/lib/git-sync-lock.sh is missing"
    src = shared.read_text()
    assert 'GIT_SYNC_LOCK_WAIT="${AE_GIT_SYNC_LOCK_WAIT:-150}"' in src
    assert "git_sync_lock_path()" in src
    assert "/tmp/nousergon-git-sync-" in src


def test_morning_signal_sync_script_exists_and_is_executable():
    sync = _INFRA_DIR / "morning-signal-sync.sh"
    assert sync.is_file(), "infrastructure/morning-signal-sync.sh is missing"
    import os

    assert os.access(sync, os.X_OK), "morning-signal-sync.sh must be executable"
    src = sync.read_text()
    assert "flock" in src
    assert "git fetch origin" in src
    assert 'git rev-parse --verify --quiet origin/main' in src, (
        "morning-signal-sync.sh must assert origin/main is present before "
        "reset --hard, not just retry the fetch"
    )


def test_morning_signal_ExecStartPre_units_keep_best_effort_prefix():
    """morning-signal.service is deliberately kept failure-tolerant (leading
    '-') on its sync ExecStartPre — episode generation must never be skipped
    by a transient sync blip. This is a settled, documented decision (not a
    gap): a sync failure past the retry still runs stale code silently,
    tracked separately. morning-signal-pull.service is the opposite by
    design — its ExecStart has NO '-', because a failed sync there is the
    unit's entire job.

    morning-signal-bakeoff.service carried the same best-effort prefix and is
    no longer part of this assertion — it is RETIRED (alpha-engine-config-
    I9457, morning-signal-I165) and no longer installed by this repo."""
    for name in ("morning-signal.service",):
        src = (_SYSTEMD_DIR / name).read_text()
        sync_lines = [
            line.strip()
            for line in src.splitlines()
            if "morning-signal-sync.sh" in line and line.strip().startswith("Exec")
        ]
        assert sync_lines, f"{name} no longer calls morning-signal-sync.sh"
        assert all(
            line.strip().startswith("ExecStartPre=-") for line in sync_lines
        ), f"{name}'s morning-signal-sync.sh call must keep the best-effort '-' prefix"

    pull_src = (_SYSTEMD_DIR / "morning-signal-pull.service").read_text()
    sync_lines = [
        line.strip()
        for line in pull_src.splitlines()
        if "morning-signal-sync.sh" in line and line.strip().startswith("Exec")
    ]
    assert sync_lines, "morning-signal-pull.service no longer calls morning-signal-sync.sh"
    assert all(
        line.strip().startswith("ExecStart=/") for line in sync_lines
    ), "morning-signal-pull.service's sync call must fail loud (no '-' prefix)"
