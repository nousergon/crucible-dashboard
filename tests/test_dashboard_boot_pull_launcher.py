"""boot-pull.service must not execute boot-pull.sh from the checkout it syncs,
and the cutover to the out-of-tree launcher must never leave a unit that
cannot start (alpha-engine-config-I8734, dashboard-box half).

Mirrors crucible-executor's tests/test_trading_box_boot_pull.py,
test_boot_pull_launcher_snapshot_freshness.py and
test_boot_pull_launcher_self_heal.py (crucible-executor-PR495/PR519/PR533).

The dashboard box adds one hazard the trading box did not have in this form:
boot-pull.sh copies every unit in infrastructure/systemd/ into
/etc/systemd/system on every HOURLY run, including its own. A
boot-pull.service whose ExecStart names /usr/local/sbin/boot-pull-launcher.sh,
copied while that file is absent, fails 203/EXEC on every later run — and
boot-pull is the thing that would have installed the launcher. So:

  * the launcher is installed BEFORE the unit sync, in boot-pull.sh and in
    install-boot-pull.sh alike;
  * the unit sync refuses to install that unit while the launcher is not an
    executable file, keeping the installed (in-tree ExecStart) unit;
  * deploy-on-merge.sh's install-boot-pull.sh row gates on the launcher too,
    so an OLD boot-pull.sh that copied the new unit mid-cutover is repaired by
    the merge's own deploy.

The guard and the install/sync block are exercised by RUNNING them against
sandbox paths, not only by reading the source.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_INFRA = _ROOT / "infrastructure"
_BOOT_PULL = _INFRA / "boot-pull.sh"
_LAUNCHER = _INFRA / "boot-pull-launcher.sh"
_SERVICE_FILE = _INFRA / "systemd" / "boot-pull.service"
_INSTALL_SCRIPT = _INFRA / "install-boot-pull.sh"
_DEPLOY = _INFRA / "deploy-on-merge.sh"
_GIT_SYNC_LOCK_LIB = _INFRA / "lib" / "git-sync-lock.sh"
_SYNCED_REPO_PREFIX = "/home/ec2-user/alpha-engine-dashboard"
_LAUNCHER_DST = "/usr/local/sbin/boot-pull-launcher.sh"
_IN_TREE_EXEC_START = f"ExecStart={_SYNCED_REPO_PREFIX}/infrastructure/boot-pull.sh"


def _exec_start_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("ExecStart="):
            return line.strip()
    raise AssertionError("no ExecStart= line found")


# ── The unit and the launcher ─────────────────────────────────────────────


def test_unit_execstart_is_outside_the_synced_repo():
    path = _exec_start_line(_SERVICE_FILE.read_text()).split("=", 1)[1].strip()
    assert not path.startswith(_SYNCED_REPO_PREFIX), (
        f"boot-pull.service ExecStart={path} is inside the checkout boot-pull.sh "
        "fast-forwards and resets while it runs"
    )
    assert path == _LAUNCHER_DST


def test_launcher_snapshot_is_outside_the_synced_repo_and_is_what_it_execs():
    src = _LAUNCHER.read_text()
    snapshot = next(
        ln for ln in src.splitlines() if ln.strip().startswith("SNAPSHOT=")
    ).split("=", 1)[1].strip().strip('"')
    assert not snapshot.startswith(_SYNCED_REPO_PREFIX)
    assert 'exec "$SNAPSHOT"' in src
    assert 'exec "$SRC"' not in src


def test_launcher_lock_is_the_one_every_other_writer_on_the_checkout_takes():
    """The launcher's fetch must flock the inode git_sync_lock_path() names
    for this checkout — a second spelling of the path is a second lock."""
    expected = subprocess.run(
        ["bash", "-c", f'. "{_GIT_SYNC_LOCK_LIB}"; git_sync_lock_path {_SYNCED_REPO_PREFIX}'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    got = subprocess.run(
        ["bash", "-c",
         f'REPO={_SYNCED_REPO_PREFIX}; '
         + next(ln for ln in _LAUNCHER.read_text().splitlines() if ln.startswith("SYNC_LOCK="))
         + '; echo "$SYNC_LOCK"'],
        capture_output=True, text=True, check=True, env={"PATH": os.environ["PATH"]},
    ).stdout.strip()
    assert got == expected


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    ).stdout.strip()


def _origin_and_clone(tmp_path: Path, body: str) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    (origin / "infrastructure").mkdir(parents=True)
    (origin / "infrastructure" / "boot-pull.sh").write_text(body)
    _git(origin, "init", "--quiet", "--initial-branch=main")
    _git(origin, "add", "-A")
    _git(origin, "commit", "--quiet", "-m", "initial")
    clone = tmp_path / "alpha-engine-dashboard"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   check=True, capture_output=True)
    return origin, clone


def _run_launcher(tmp_path: Path, repo: Path, snapshot: Path):
    """The real launcher with its hardcoded paths rebound to the sandbox and
    the `sudo -u ... flock ...` prefix reduced to plain git (unprivileged CI)."""
    src = _LAUNCHER.read_text()
    for old, new in (
        (f'SRC="{_SYNCED_REPO_PREFIX}/infrastructure/boot-pull.sh"',
         f'SRC="{repo}/infrastructure/boot-pull.sh"'),
        ('SNAPSHOT="/home/ec2-user/.boot-pull-snapshot.sh"', f'SNAPSHOT="{snapshot}"'),
        ('sudo -u "$RUN_AS" -H flock -w 150 "$SYNC_LOCK" git -C "$REPO" "$@"',
         'git -C "$REPO" "$@"'),
    ):
        assert old in src, f"launcher no longer contains {old!r}"
        src = src.replace(old, new)
    sandboxed = tmp_path / "boot-pull-launcher.sh"
    sandboxed.write_text(src)
    sandboxed.chmod(0o755)
    return subprocess.run(
        ["bash", str(sandboxed)], capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AE_LAUNCHER_REPO": str(repo)},
    )


def test_launcher_runs_origin_main_not_the_stale_tree_and_leaves_head_alone(tmp_path):
    origin, repo = _origin_and_clone(tmp_path, "#!/bin/bash\necho RAN_STALE\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    (origin / "infrastructure" / "boot-pull.sh").write_text("#!/bin/bash\necho RAN_FIXED\n")
    _git(origin, "commit", "--quiet", "-am", "fix")

    snapshot = tmp_path / "snap" / "boot-pull-snapshot.sh"
    snapshot.parent.mkdir()
    res = _run_launcher(tmp_path, repo, snapshot)

    assert res.returncode == 0, res.stderr
    assert "RAN_FIXED" in res.stdout and "RAN_STALE" not in res.stdout
    assert "SNAPSHOT WAS STALE" in res.stderr
    assert snapshot.is_file() and not str(snapshot).startswith(str(repo))
    # boot-pull.sh's health-gate revert target is the HEAD it records itself.
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_launcher_falls_back_to_the_on_disk_copy_when_fetch_fails(tmp_path):
    _origin, repo = _origin_and_clone(tmp_path, "#!/bin/bash\necho RAN_FROM_DISK\n")
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "no-such-origin"))
    res = _run_launcher(tmp_path, repo, tmp_path / "boot-pull-snapshot.sh")
    assert res.returncode == 0, res.stderr
    assert "RAN_FROM_DISK" in res.stdout
    assert "falling back to the on-disk copy" in res.stderr


# ── Ordering: launcher before unit, in every writer ──────────────────────


def test_installer_installs_and_verifies_the_launcher_before_copying_units():
    src = _INSTALL_SCRIPT.read_text()
    install_pos = src.index('install -m 0755 -o root -g root "$LAUNCHER_SRC" "$LAUNCHER_DST"')
    verify_pos = src.index('if [ ! -x "$LAUNCHER_DST" ]')
    unit_copy_pos = src.index('cp "$src" "$dst"')
    assert install_pos < verify_pos < unit_copy_pos
    assert f'LAUNCHER_DST="{_LAUNCHER_DST}"' in src
    assert "set -euo pipefail" in src, "a failed install must stop before the unit copy"


def test_boot_pull_installs_the_launcher_before_the_unit_sync():
    src = _BOOT_PULL.read_text()
    install_pos = src.index('sudo install -m 0755 -o root -g root "$LAUNCHER_SRC" "$LAUNCHER_DST"')
    unit_sync_pos = src.index('SYNC $name (updated)')
    assert install_pos < unit_sync_pos
    assert f'LAUNCHER_DST="${{AE_BOOT_PULL_LAUNCHER_DST:-{_LAUNCHER_DST}}}"' in src


def test_every_unit_copy_from_this_repo_is_behind_the_launcher_guard():
    """Both the forward sync and the health-gate REVERT-SYNC copy
    boot-pull.service; each must consult the guard before its `sudo cp`."""
    src = _BOOT_PULL.read_text()
    loops = [m.start() for m in re.finditer(
        r'for unit in "\$SYSTEMD_SRC"/\*\.service "\$SYSTEMD_SRC"/\*\.timer; do', src)]
    assert len(loops) == 2, "expected the forward sync and the REVERT-SYNC loops"
    for start in loops:
        body = src[start:src.index("done", start)]
        assert body.index('boot_pull_unit_installable "$unit"') < body.index("sudo cp"), body


def test_deploy_on_merge_reruns_the_installer_when_the_launcher_drifts():
    sh = _DEPLOY.read_text()
    row = next(ln for ln in sh.splitlines() if ln.strip().startswith('"install-boot-pull.sh|'))
    assert f"boot-pull-launcher.sh:{_LAUNCHER_DST}" in row


# ── Behaviour: the guard, and the install+sync block end to end ──────────


def _env(tmp_path: Path, launcher_dst: Path, extra_path: Path | None = None) -> dict:
    env = {
        **os.environ,
        "AE_BOOT_PULL_LIB_ONLY": "1",
        "AE_BOOT_PULL_LOG": str(tmp_path / "boot-pull.log"),
        "AE_BOOT_PULL_LAUNCHER_DST": str(launcher_dst),
        "CHECKOUT_ROSTER": str(tmp_path / "no-roster.json"),
    }
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{os.environ['PATH']}"
    return env


def _unit_text(exec_start: str) -> str:
    return re.sub(r"(?m)^ExecStart=.*$", exec_start, _SERVICE_FILE.read_text())


def _installable(tmp_path: Path, unit: Path, launcher_dst: Path) -> bool:
    res = subprocess.run(
        ["bash", "-c", f'. "{_BOOT_PULL}"; boot_pull_unit_installable "{unit}"'],
        env=_env(tmp_path, launcher_dst), capture_output=True, text=True,
    )
    return res.returncode == 0


def test_guard_refuses_a_launcher_unit_until_the_launcher_is_executable(tmp_path):
    dst = tmp_path / "sbin" / "boot-pull-launcher.sh"
    dst.parent.mkdir()
    unit = tmp_path / "src" / "boot-pull.service"
    unit.parent.mkdir()
    unit.write_text(_unit_text(f"ExecStart={dst}"))

    assert not _installable(tmp_path, unit, dst), "launcher absent"
    dst.write_text("#!/bin/bash\n")
    dst.chmod(0o644)
    assert not _installable(tmp_path, unit, dst), "launcher present but not executable"
    dst.chmod(0o755)
    assert _installable(tmp_path, unit, dst)


def test_guard_never_blocks_an_in_tree_unit_or_any_other_unit(tmp_path):
    dst = tmp_path / "sbin" / "absent-launcher.sh"
    in_tree = tmp_path / "a" / "boot-pull.service"
    in_tree.parent.mkdir()
    in_tree.write_text(_unit_text(_IN_TREE_EXEC_START))
    other = tmp_path / "b" / "box-health.service"
    other.parent.mkdir()
    other.write_text(f"[Service]\nExecStart={dst}\n")
    assert _installable(tmp_path, in_tree, dst)
    assert _installable(tmp_path, other, dst)


def _sync_block_harness(tmp_path: Path, *, fail_install: bool):
    """Run boot-pull.sh's real launcher-install + unit-sync block against a
    sandbox /etc/systemd/system that holds today's in-tree unit, with `sudo`
    shimmed (install can be made to fail; systemctl is a no-op)."""
    src_dir = tmp_path / "repo-systemd"
    etc = tmp_path / "etc-systemd-system"
    sbin = tmp_path / "sbin"
    for d in (src_dir, etc, sbin):
        d.mkdir()
    launcher_src = tmp_path / "boot-pull-launcher.sh"
    launcher_src.write_text(_LAUNCHER.read_text())
    launcher_dst = sbin / "boot-pull-launcher.sh"

    new_unit = _unit_text(f"ExecStart={launcher_dst}")
    old_unit = _unit_text(_IN_TREE_EXEC_START)
    (src_dir / "boot-pull.service").write_text(new_unit)
    (etc / "boot-pull.service").write_text(old_unit)
    timer = (_INFRA / "systemd" / "boot-pull.timer").read_text()
    (src_dir / "boot-pull.timer").write_text(timer)
    (etc / "boot-pull.timer").write_text(timer)

    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sudo").write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        f'  install) {"exit 1" if fail_install else ""}\n'
        '    shift; while [ $# -gt 2 ]; do shift; done; exec install -m 0755 "$1" "$2" ;;\n'
        "  systemctl) exit 0 ;;\n"
        '  *) exec "$@" ;;\n'
        "esac\n"
    )
    (shim / "sudo").chmod(0o755)

    src = _BOOT_PULL.read_text()
    block = src[src.index("# ── Self-heal boot-pull-launcher.sh"):src.index("# ── Sync metron-intraday")]
    old_src = f'SYSTEMD_SRC="{_SYNCED_REPO_PREFIX}/infrastructure/systemd"'
    assert old_src in block
    block = block.replace(old_src, f'SYSTEMD_SRC="{src_dir}"').replace(
        "/etc/systemd/system", str(etc))
    (tmp_path / "block.sh").write_text(block)

    res = subprocess.run(
        ["bash", "-c",
         f'. "{_BOOT_PULL}"; PULL_FAILURES=0; FAILED_REPOS=(); RESTARTED_SERVICES=(); '
         f'LAUNCHER_SRC="{launcher_src}"; . "{tmp_path / "block.sh"}"; '
         'echo "PF=$PULL_FAILURES"; printf "FR=%s\\n" "${FAILED_REPOS[@]}"'],
        env=_env(tmp_path, launcher_dst, shim), capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    log = (tmp_path / "boot-pull.log").read_text()
    return res.stdout, log, etc / "boot-pull.service", launcher_dst, new_unit, old_unit


def test_failed_launcher_install_keeps_the_startable_in_tree_unit(tmp_path):
    out, log, installed, launcher_dst, _new, old_unit = _sync_block_harness(
        tmp_path, fail_install=True)
    assert installed.read_text() == old_unit, (
        "the unit sync installed a boot-pull.service whose ExecStart does not exist"
    )
    assert not launcher_dst.exists()
    assert "PF=2" in out, out
    assert "FR=boot-pull-launcher (install)" in out
    assert "FR=boot-pull.service (launcher missing)" in out
    assert "FAIL boot-pull-launcher" in log
    assert "held back" in log


def test_successful_launcher_install_then_syncs_the_launcher_unit(tmp_path):
    out, log, installed, launcher_dst, new_unit, _old = _sync_block_harness(
        tmp_path, fail_install=False)
    assert os.access(launcher_dst, os.X_OK)
    assert launcher_dst.read_text() == _LAUNCHER.read_text()
    assert installed.read_text() == new_unit
    assert "PF=0" in out, out
    assert log.index("OK   boot-pull-launcher: installed") < log.index("SYNC boot-pull.service (updated)")
