"""Tests for infrastructure/scan_unlisted_state.py (T1-4, alpha-engine-config-I6719).

Regression coverage for the gap this closes: backup_box_state.py replicates
what budget.yaml::state[] declares, but nothing scanned the box for
state-shaped files nobody declared at all. Mirrors test_check_package_drift.py's
import-by-path pattern (`infrastructure/` isn't itself pytest-collected).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "infrastructure" / "scan_unlisted_state.py"
)
_spec = importlib.util.spec_from_file_location("scan_unlisted_state", _MODULE_PATH)
sus = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("scan_unlisted_state", sus)
_spec.loader.exec_module(sus)

HAS_GIT = shutil.which("git") is not None


# ── is_state_shaped ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "auth.db", "vires.db", "app.sqlite", "app.sqlite3",
    "alpha-engine-key.pem", "id.key", "SomeVault.json", "credentials_vault",
    "AUTH.DB",  # case-insensitive
])
def test_is_state_shaped_matches_known_shapes(name):
    assert sus.is_state_shaped(name)


@pytest.mark.parametrize("name", [
    "readme.md", "app.py", "config.yaml", "notes.txt", "data.csv", "script.sh",
])
def test_is_state_shaped_rejects_non_state_files(name):
    assert not sus.is_state_shaped(name)


# ── matches_any (declared/allowlist glob semantics) ────────────────────────

def test_matches_any_exact_path():
    assert sus.matches_any("/home/ec2-user/vires/vires.db", ["/home/ec2-user/vires/vires.db"])


def test_matches_any_directory_prefix_covers_children():
    # Mirrors box_health.sh's `case "$f" in $d|$d*)` — a declared entry naming
    # a directory covers everything under it without a trailing glob.
    assert sus.matches_any(
        "/home/ec2-user/foo/nested/bar.sqlite", ["/home/ec2-user/foo/"]
    )


def test_matches_any_glob_pattern():
    assert sus.matches_any("/home/ec2-user/metron/.env", ["/home/ec2-user/*/.env"])


def test_matches_any_no_match():
    assert not sus.matches_any("/home/ec2-user/other/x.db", ["/home/ec2-user/vires/vires.db"])


# ── find_state_shaped_files / directory pruning ─────────────────────────────

def test_planted_undeclared_sqlite_is_found(tmp_path):
    planted = tmp_path / "somesvc" / "planted.sqlite"
    planted.parent.mkdir(parents=True)
    planted.write_text("x")

    found = sus.find_state_shaped_files([tmp_path])
    assert planted in found


def test_excluded_dir_names_are_pruned(tmp_path):
    for d in (".venv", "node_modules", ".git", "__pycache__"):
        p = tmp_path / d / "hidden.sqlite"
        p.parent.mkdir(parents=True)
        p.write_text("x")

    found = sus.find_state_shaped_files([tmp_path])
    assert found == []


def test_vendor_path_substring_is_pruned(tmp_path):
    p = tmp_path / "opt" / "aws" / "lib" / "cache.db"
    p.parent.mkdir(parents=True)
    p.write_text("x")

    found = sus.find_state_shaped_files(
        [tmp_path], exclude_path_substrings=(f"{tmp_path}/opt/aws/",)
    )
    assert found == []


def test_non_state_files_are_not_collected(tmp_path):
    (tmp_path / "readme.md").write_text("x")
    (tmp_path / "app.py").write_text("x")

    assert sus.find_state_shaped_files([tmp_path]) == []


def test_nonexistent_root_is_skipped_not_fatal(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert sus.find_state_shaped_files([missing]) == []


# ── scan(): the three exclusion filters ─────────────────────────────────────

def test_registry_declared_path_is_not_flagged(tmp_path):
    declared_file = tmp_path / "home" / "nousergon-auth" / "auth.sqlite"
    declared_file.parent.mkdir(parents=True)
    declared_file.write_text("x")

    unlisted = sus.scan(
        roots=[tmp_path],
        declared_patterns=[str(declared_file)],
        allowlist_entries=[],
        git_tracked=set(),
    )
    assert unlisted == []


def test_allowlisted_path_is_not_flagged(tmp_path):
    allowed = tmp_path / "morning-signal" / "flow-doctor.db"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("x")

    unlisted = sus.scan(
        roots=[tmp_path],
        declared_patterns=[],
        allowlist_entries=[
            {"path": str(allowed), "rpo": "24h", "rationale": "regenerates on next run"}
        ],
        git_tracked=set(),
    )
    assert unlisted == []


def test_planted_undeclared_sqlite_is_found_via_scan(tmp_path):
    planted = tmp_path / "somesvc" / "planted.sqlite"
    planted.parent.mkdir(parents=True)
    planted.write_text("x")

    unlisted = sus.scan(
        roots=[tmp_path], declared_patterns=[], allowlist_entries=[], git_tracked=set()
    )
    assert unlisted == [planted]


def test_git_tracked_state_shaped_file_is_excluded_from_scan(tmp_path):
    # A file matching neither the registry nor the allowlist, but reported as
    # git-tracked, must still be excluded — it's a test fixture in a repo's
    # tracked tree, not box state.
    tracked = tmp_path / "some-repo" / "tests" / "fixture.sqlite"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("x")

    unlisted = sus.scan(
        roots=[tmp_path],
        declared_patterns=[],
        allowlist_entries=[],
        git_tracked={str(tracked)},
    )
    assert unlisted == []


def test_mixed_declared_allowlisted_and_unlisted(tmp_path):
    declared = tmp_path / "a" / "declared.db"
    allowed = tmp_path / "b" / "allowed.sqlite"
    rogue = tmp_path / "c" / "rogue.pem"
    for p in (declared, allowed, rogue):
        p.parent.mkdir(parents=True)
        p.write_text("x")

    unlisted = sus.scan(
        roots=[tmp_path],
        declared_patterns=[str(declared)],
        allowlist_entries=[{"path": str(allowed), "rpo": "total", "rationale": "n/a"}],
        git_tracked=set(),
    )
    assert unlisted == [rogue]


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_discover_git_tracked_finds_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "fixture.sqlite"
    tracked.write_text("x")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "fixture.sqlite"], check=True)

    tracked_set = sus.discover_git_tracked([tmp_path])
    assert str(tracked.resolve()) in {str(Path(p).resolve()) for p in tracked_set} \
        or str(tracked) in tracked_set


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_end_to_end_scan_excludes_a_real_git_tracked_fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "fixture.sqlite"
    tracked.write_text("x")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "fixture.sqlite"], check=True)

    untracked = repo / "planted.sqlite"
    untracked.write_text("x")

    unlisted = sus.scan(roots=[tmp_path], declared_patterns=[], allowlist_entries=[])
    unlisted_names = {p.name for p in unlisted}
    assert "planted.sqlite" in unlisted_names
    assert "fixture.sqlite" not in unlisted_names


# ── registry loaders ─────────────────────────────────────────────────────────

def test_load_declared_paths_reads_every_disposition(tmp_path):
    budget = tmp_path / "budget.yaml"
    budget.write_text(
        "state:\n"
        "  - path: /a/replicate.db\n"
        "    disposition: replicate\n"
        "  - path: /b/external.db\n"
        "    disposition: external\n"
        "  - path: /c/accepted.db\n"
        "    disposition: accepted-loss\n"
        "    rpo: total\n"
    )
    paths = sus.load_declared_paths(budget)
    assert paths == ["/a/replicate.db", "/b/external.db", "/c/accepted.db"]


def test_load_declared_paths_raises_on_missing_path_field(tmp_path):
    budget = tmp_path / "budget.yaml"
    budget.write_text("state:\n  - disposition: replicate\n")
    with pytest.raises(ValueError, match="missing path"):
        sus.load_declared_paths(budget)


def test_load_declared_paths_empty_state_is_empty_list(tmp_path):
    budget = tmp_path / "budget.yaml"
    budget.write_text("services: []\n")
    assert sus.load_declared_paths(budget) == []


def test_load_allowlist_missing_file_is_empty(tmp_path):
    assert sus.load_allowlist(tmp_path / "does-not-exist.yaml") == []


def test_load_allowlist_valid_entries(tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(
        "entries:\n"
        "  - path: /home/ec2-user/x/y.db\n"
        "    rpo: 24h\n"
        "    rationale: regenerates on next run\n"
    )
    entries = sus.load_allowlist(allowlist)
    assert len(entries) == 1
    assert entries[0]["path"] == "/home/ec2-user/x/y.db"


@pytest.mark.parametrize("missing_field", ["path", "rpo", "rationale"])
def test_load_allowlist_raises_on_missing_required_field(tmp_path, missing_field):
    entry = {"path": "/a/b.db", "rpo": "24h", "rationale": "why"}
    del entry[missing_field]
    body = "\n    ".join(f"{k}: {v}" for k, v in entry.items())
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(f"entries:\n  - {body}\n")
    with pytest.raises(ValueError, match="missing"):
        sus.load_allowlist(allowlist)


def test_load_allowlist_empty_entries_list(tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("entries: []\n")
    assert sus.load_allowlist(allowlist) == []


# ── main(): exit codes and CLI wiring ───────────────────────────────────────

def _write_budget(tmp_path, state_lines=""):
    budget = tmp_path / "budget.yaml"
    budget.write_text(f"state:\n{state_lines}" if state_lines else "state: []\n")
    return budget


def test_main_exits_zero_when_clean(tmp_path, monkeypatch, capsys):
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir()
    budget = _write_budget(tmp_path)
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("entries: []\n")

    monkeypatch.setattr(sus, "emit_metric", lambda count: None)
    monkeypatch.setattr(sus, "publish_alert", lambda findings: (_ for _ in ()).throw(
        AssertionError("publish_alert must not be called on a clean scan")))

    rc = sus.main([
        "--budget", str(budget), "--allowlist", str(allowlist),
        "--root", str(scan_root),
    ])
    assert rc == 0
    assert "0 unlisted" in capsys.readouterr().out


def test_main_exits_one_and_alerts_on_findings(tmp_path, monkeypatch, capsys):
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir()
    (scan_root / "rogue.sqlite").write_text("x")
    budget = _write_budget(tmp_path)
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("entries: []\n")

    metric_calls = []
    alert_calls = []
    monkeypatch.setattr(sus, "emit_metric", lambda count: metric_calls.append(count))
    monkeypatch.setattr(sus, "publish_alert", lambda findings: alert_calls.append(findings))

    rc = sus.main([
        "--budget", str(budget), "--allowlist", str(allowlist),
        "--root", str(scan_root),
    ])
    assert rc == 1
    assert metric_calls == [1]
    assert len(alert_calls) == 1
    assert "FAILED unlisted state" in capsys.readouterr().err


def test_main_dry_run_never_emits_metric_or_alert(tmp_path, monkeypatch):
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir()
    (scan_root / "rogue.sqlite").write_text("x")
    budget = _write_budget(tmp_path)
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("entries: []\n")

    monkeypatch.setattr(sus, "emit_metric", lambda count: (_ for _ in ()).throw(
        AssertionError("--dry-run must not publish a metric")))
    monkeypatch.setattr(sus, "publish_alert", lambda findings: (_ for _ in ()).throw(
        AssertionError("--dry-run must not publish an alert")))

    rc = sus.main([
        "--budget", str(budget), "--allowlist", str(allowlist),
        "--root", str(scan_root), "--dry-run",
    ])
    assert rc == 1  # findings still fail the run even in dry-run


def test_main_exits_two_on_malformed_registry(tmp_path, capsys):
    scan_root = tmp_path / "scanroot"
    scan_root.mkdir()
    budget = tmp_path / "budget.yaml"
    budget.write_text("state:\n  - disposition: replicate\n")  # missing path
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("entries: []\n")

    rc = sus.main([
        "--budget", str(budget), "--allowlist", str(allowlist),
        "--root", str(scan_root),
    ])
    assert rc == 2
    assert "could not read registry" in capsys.readouterr().err
