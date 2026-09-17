"""boot-pull's per-checkout roster pull now retries a transient remote
failure (alpha-engine-config-I10950).

Measured 2026-09-17: `boot-pull.service` FAILED at 04:41:57 UTC on the
dashboard box (i-09b539c844515d549). Both /home/ec2-user/alpha-engine-config
and /home/ec2-user/telos-ops hit a credential-cache miss that re-minted a
token GitHub then answered "Repository not found" for exactly that one run;
claude-code-config and metron-ops hit the identical cache-miss branch in the
SAME run and succeeded, and the very next scheduled run (61 minutes later)
pulled all 18 roster checkouts cleanly. sync_repo_to_main() (the fetch +
`merge --ff-only` decision, alpha-engine-config-I10260) had no retry at all
on this path — unlike crucible-executor's boot-pull.sh, which already
retries a failed `main` fetch once for a different (ref compare-and-swap)
failure class.

Exercised by RUNNING sync_repo_to_main() and its helpers, sourced from the
real script via AE_BOOT_PULL_LIB_ONLY=1 (same idiom as
crucible-executor/tests/test_boot_pull_post_condition.py, and this repo's own
test_boot_pull_checkout_roster.py pins the source-text side of the same
function) — a source-text regex cannot see the retry loop's control flow or
the backoff/erase sequencing, which is exactly what this defect and its fix
live in.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_BOOT_PULL = Path(__file__).parent.parent / "infrastructure" / "boot-pull.sh"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **_GIT_ENV},
        capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _shim_dir(tmp_path: Path, name: str) -> Path:
    """PATH-prepended dir carrying a `flock` stand-in on platforms lacking it
    (macOS) — same rationale and shape as crucible-executor's test helper of
    the same name: it drops `-w <secs> <lockfile>` and execs the rest, so the
    retry logic under test — not flock's availability — is what each case
    exercises.
    """
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    if shutil.which("flock") is None:
        (d / "flock").write_text(
            "#!/bin/sh\n"
            'while [ "$1" = "-w" ]; do shift 2; done\n'
            "shift\n"
            'exec "$@"\n'
        )
        (d / "flock").chmod(0o755)
    return d


@pytest.fixture
def checkout(tmp_path: Path):
    """A checkout on main, one commit behind a real local bare `origin` —
    real fetch/merge succeeds against it exactly like the box's own
    checkouts, `git fetch`/`merge --ff-only` unshimmed. Individual tests
    shim `git fetch` on top of this to control failure/recovery.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", ".", cwd=remote)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--initial-branch=main", ".", cwd=seed)
    (seed / "f.txt").write_text("v1\n")
    _git("add", "f.txt", cwd=seed)
    _git("commit", "-m", "v1", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    box = tmp_path / "alpha-engine-data"
    _git("clone", str(remote), str(box), cwd=tmp_path)

    (seed / "f.txt").write_text("v2\n")
    _git("commit", "-am", "v2", cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    return {"checkout": box, "remote": remote, "tmp": tmp_path}


def _install_git_shim(tmp_path: Path, *, name: str, fetch_outcomes: list[str]) -> Path:
    """A PATH-shadowing `git` whose `fetch` calls are scripted by
    `fetch_outcomes` (one entry per call number; the last entry repeats once
    exhausted). An entry is either the literal "ok" (forward to the real
    `git fetch`, which reaches the fixture's real local bare remote) or a
    stderr message to emit before exiting 1. Every non-fetch invocation
    (merge, status, rev-parse, remote, ...) always passes straight through.
    """
    real_git = shutil.which("git")
    assert real_git, "git must be on PATH"
    shim_dir = _shim_dir(tmp_path, name)
    counter = shim_dir / "fetch_count"

    branches = []
    for i, outcome in enumerate(fetch_outcomes, start=1):
        cond = f'[ "$n" -eq {i} ]' if i < len(fetch_outcomes) else f'[ "$n" -ge {i} ]'
        if outcome == "ok":
            branches.append(f'if {cond}; then exec "{real_git}" "$@"; fi')
        else:
            escaped = outcome.replace('"', '\\"')
            branches.append(f'if {cond}; then echo "{escaped}" >&2; exit 1; fi')
    body = "\n    ".join(branches)

    (shim_dir / "git").write_text(
        "#!/bin/sh\n"
        f'COUNT_FILE="{counter}"\n'
        'case " $* " in\n'
        '*" fetch "*)\n'
        '    n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\n'
        '    n=$((n + 1))\n'
        '    echo "$n" > "$COUNT_FILE"\n'
        f"    {body}\n"
        '    ;;\n'
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    (shim_dir / "git").chmod(0o755)
    return shim_dir


def _fetch_count(shim: Path) -> int:
    f = shim / "fetch_count"
    return int(f.read_text().strip()) if f.exists() else 0


def _run_lib(call: str, tmp_path: Path, *, shim: Path | None = None,
             cred_helper: str | None = None, extra_env: dict | None = None):
    """Source boot-pull.sh in lib-only mode and evaluate `call` (a shell
    expression invoking one of its functions)."""
    log = tmp_path / "boot-pull.log"
    env = {
        **os.environ, **_GIT_ENV,
        "AE_BOOT_PULL_LIB_ONLY": "1",
        "AE_BOOT_PULL_LOG": str(log),
        "AE_GIT_SYNC_LOCK_WAIT": "10",
        "AE_BOOT_PULL_RETRY_BACKOFF_1": "0",
        "AE_BOOT_PULL_RETRY_BACKOFF_2": "0",
    }
    if shim is not None:
        env["PATH"] = f"{shim}{os.pathsep}{os.environ['PATH']}"
    if cred_helper is not None:
        env["AE_CRED_HELPER"] = cred_helper
    if extra_env:
        env.update(extra_env)
    res = subprocess.run(
        ["bash", "-c", f'. "{_BOOT_PULL}"; {call}'],
        env=env, capture_output=True, text=True,
    )
    return res.returncode, (log.read_text() if log.exists() else ""), res.stderr


def _sync(checkout: Path, tmp_path: Path, *, shim: Path, cred_helper: str | None = None):
    return _run_lib(f'sync_repo_to_main "{checkout}"', tmp_path, shim=shim, cred_helper=cred_helper)


def _fake_cred_helper(tmp_path: Path, *, calls_file: Path) -> str:
    """A stand-in `git-credential-nousergon-app` recording every `erase`
    invocation's stdin (the protocol/host/path block) to `calls_file`.
    """
    helper = tmp_path / "fake-cred-helper.sh"
    helper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "erase" ]; then cat >> "{calls_file}"; echo "---" >> "{calls_file}"; fi\n'
        "exit 0\n"
    )
    helper.chmod(0o755)
    return str(helper)


class TestTransientClassRetries:
    def test_transient_signature_retries_then_succeeds(self, checkout, tmp_path):
        shim = _install_git_shim(
            tmp_path, name="shim-transient",
            fetch_outcomes=[
                "fatal: unable to access 'https://github.com/nousergon/nousergon-data.git/': Could not resolve host",
                "ok",
            ],
        )
        cred_calls = tmp_path / "cred_calls.txt"
        helper = _fake_cred_helper(tmp_path, calls_file=cred_calls)

        rc, log, stderr = _sync(checkout["checkout"], tmp_path, shim=shim, cred_helper=helper)

        assert rc == 0, f"a matched transient class must recover; log:\n{log}\nstderr:\n{stderr}"
        assert _fetch_count(shim) == 2, "must retry exactly once to recover"
        assert "RETRY" in log and "attempt 1/3" in log
        assert "Could not resolve host" in log
        assert "recovered on attempt 2/3" in log
        # The credential cache must be invalidated before the retry.
        assert cred_calls.exists(), "a transient retry must erase the cached credential first"

    def test_missing_helper_binary_does_not_convert_retryable_to_hard_failure(self, checkout, tmp_path):
        shim = _install_git_shim(
            tmp_path, name="shim-transient-nohelper",
            fetch_outcomes=["fatal: RPC failed; curl 56", "ok"],
        )
        missing_helper = str(tmp_path / "does-not-exist" / "git-credential-nousergon-app")

        rc, log, stderr = _sync(checkout["checkout"], tmp_path, shim=shim, cred_helper=missing_helper)

        assert rc == 0, f"a missing helper must not block the retry's own recovery; log:\n{log}\nstderr:\n{stderr}"
        assert "SKIP credential erase" in log
        assert _fetch_count(shim) == 2

    def test_exhausted_transient_retries_still_fails(self, checkout, tmp_path):
        # Every attempt matches the transient class, but the remote never
        # actually recovers (a genuinely dead endpoint) — 3 attempts, then FAIL.
        shim = _install_git_shim(
            tmp_path, name="shim-exhausted",
            fetch_outcomes=["fatal: Could not resolve host: github.com"] * 3,
        )
        rc, log, stderr = _sync(checkout["checkout"], tmp_path, shim=shim,
                                 cred_helper=str(tmp_path / "missing-helper"))

        assert rc == 1, f"log:\n{log}\nstderr:\n{stderr}"
        assert _fetch_count(shim) == 3, "must attempt exactly 3 times, never more"
        assert "exhausted retrying transient class" in log


class TestNonTransientNeverRetries:
    def test_unmatched_failure_is_not_retried(self, checkout, tmp_path):
        shim = _install_git_shim(
            tmp_path, name="shim-nontransient",
            fetch_outcomes=["fatal: bad object refs/heads/main; local corruption"],
        )
        cred_calls = tmp_path / "cred_calls.txt"
        helper = _fake_cred_helper(tmp_path, calls_file=cred_calls)

        rc, log, stderr = _sync(checkout["checkout"], tmp_path, shim=shim, cred_helper=helper)

        assert rc == 1, f"a non-transient failure must fail on attempt 1; log:\n{log}\nstderr:\n{stderr}"
        assert _fetch_count(shim) == 1, "must not retry a signature that isn't in the transient list"
        assert "no known transient signature" in log
        assert "RETRY" not in log
        assert not cred_calls.exists(), "no retry means no credential erase either"

    def test_dirty_tree_is_never_retried(self, checkout, tmp_path):
        # A dirty tracked file makes `merge --ff-only` refuse even though the
        # fetch itself succeeds cleanly — sync_repo_to_main must return 3
        # (dirty-tree) on the FIRST attempt, never treat it as transient.
        (checkout["checkout"] / "f.txt").write_text("locally modified\n")
        shim = _install_git_shim(tmp_path, name="shim-dirty", fetch_outcomes=["ok"])

        rc, log, stderr = _sync(checkout["checkout"], tmp_path, shim=shim,
                                 cred_helper=str(tmp_path / "missing-helper"))

        assert rc == 3, f"a dirty tracked file must return 3, not retry; log:\n{log}\nstderr:\n{stderr}"
        assert _fetch_count(shim) == 1
        assert "RETRY" not in log


class TestCredentialEraseKeysOnRemoteSlug:
    """_boot_pull_erase_credential (called mid-retry by sync_repo_to_main
    above) must derive the credential-cache path from the checkout's OWN
    `origin` remote, never its directory name — the directory name is not
    the repo slug (`alpha-engine-data` is `nousergon/nousergon-data`).
    Exercised directly: this function never fetches, so it needs no real
    remote to reach, only a `git remote` to read.
    """

    def test_erase_derives_slug_from_origin_not_dirname(self, tmp_path):
        repo = tmp_path / "alpha-engine-data"
        repo.mkdir()
        _git("init", "--initial-branch=main", ".", cwd=repo)
        _git("remote", "add", "origin", "https://github.com/nousergon/nousergon-data.git", cwd=repo)

        cred_calls = tmp_path / "cred_calls.txt"
        helper = _fake_cred_helper(tmp_path, calls_file=cred_calls)

        rc, log, stderr = _run_lib(
            f'_boot_pull_erase_credential "{repo}"', tmp_path, cred_helper=helper,
        )

        assert rc == 0, f"stderr:\n{stderr}"
        recorded = cred_calls.read_text()
        assert "path=nousergon/nousergon-data" in recorded, (
            f"erase must key on the ORIGIN remote's slug, not the checkout's "
            f"directory name (`alpha-engine-data`); recorded:\n{recorded}"
        )
        assert "alpha-engine-data" not in recorded

    def test_missing_helper_is_skipped_not_failed(self, tmp_path):
        repo = tmp_path / "somechk"
        repo.mkdir()
        _git("init", "--initial-branch=main", ".", cwd=repo)
        _git("remote", "add", "origin", "https://github.com/nousergon/somechk.git", cwd=repo)

        rc, log, stderr = _run_lib(
            f'_boot_pull_erase_credential "{repo}"', tmp_path,
            cred_helper=str(tmp_path / "does-not-exist"),
        )

        assert rc == 0, f"a missing helper must be a no-op, not a failure; stderr:\n{stderr}"
        assert "SKIP credential erase" in log

    def test_unresolvable_remote_is_skipped_not_failed(self, tmp_path):
        repo = tmp_path / "noorigin"
        repo.mkdir()
        _git("init", "--initial-branch=main", ".", cwd=repo)
        # No `origin` remote configured at all.
        helper = _fake_cred_helper(tmp_path, calls_file=tmp_path / "cred_calls.txt")

        rc, log, stderr = _run_lib(
            f'_boot_pull_erase_credential "{repo}"', tmp_path, cred_helper=helper,
        )

        assert rc == 0, f"stderr:\n{stderr}"
        assert "skipping credential erase" in log
        assert not (tmp_path / "cred_calls.txt").exists()
