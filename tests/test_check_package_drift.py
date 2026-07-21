"""Tests for infrastructure/check_package_drift.py (config#3157).

Regression coverage for the package-version-drift gap this closes: the
box ran krepis 0.14.0 for days after requirements.in's floor said
`krepis[openai]>=0.16.2`, caught only by a human manually running
`krepis.__version__`. These tests exercise `check_package_drift()` against
a scratch requirements.in with a monkeypatched `importlib.metadata.version`
so no real package install is needed.

`infrastructure/` isn't itself pytest-collected (its scripts are bash;
see infrastructure/test_deploy_on_merge_paths_changed.sh's own docstring
noting no pytest harness covers that shell logic), but
check_package_drift.py is plain Python with no dashboard/streamlit
dependency, so it's imported directly by path here rather than requiring
a package install of the infrastructure/ dir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "infrastructure" / "check_package_drift.py"
)
_spec = importlib.util.spec_from_file_location("check_package_drift", _MODULE_PATH)
check_package_drift_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_package_drift", check_package_drift_mod)
_spec.loader.exec_module(check_package_drift_mod)

check_package_drift = check_package_drift_mod.check_package_drift

_REQUIREMENTS_IN = """\
streamlit>=1.40
krepis[openai]>=0.16.2
nousergon-lib[flow-doctor,github_app] @ git+https://github.com/nousergon/nousergon-lib@v0.124.0
"""


@pytest.fixture()
def requirements_in(tmp_path):
    path = tmp_path / "requirements.in"
    path.write_text(_REQUIREMENTS_IN)
    return path


def _patch_versions(monkeypatch, versions: dict[str, str]):
    def fake_version(pkg):
        normalized = pkg.replace("_", "-").lower()
        for name, ver in versions.items():
            if name.replace("_", "-").lower() == normalized:
                return ver
        from importlib import metadata

        raise metadata.PackageNotFoundError(pkg)

    monkeypatch.setattr(check_package_drift_mod.metadata, "version", fake_version)


def test_pass_case_installed_versions_satisfy_declared_constraints(
    requirements_in, monkeypatch
):
    """krepis floor satisfied + nousergon-lib tag matches -> no violations."""
    _patch_versions(
        monkeypatch, {"krepis": "0.16.2", "nousergon-lib": "0.124.0"}
    )
    violations = check_package_drift(requirements_in=requirements_in)
    assert violations == []


def test_pass_case_krepis_above_floor(requirements_in, monkeypatch):
    """A newer krepis than the floor is not drift."""
    _patch_versions(
        monkeypatch, {"krepis": "0.17.0", "nousergon-lib": "0.124.0"}
    )
    violations = check_package_drift(requirements_in=requirements_in)
    assert violations == []


def test_fail_case_krepis_below_floor_is_the_actual_incident(
    requirements_in, monkeypatch
):
    """The exact config#3157 incident: krepis 0.14.0 installed, floor 0.16.2."""
    _patch_versions(
        monkeypatch, {"krepis": "0.14.0", "nousergon-lib": "0.124.0"}
    )
    violations = check_package_drift(requirements_in=requirements_in)
    assert len(violations) == 1
    assert "krepis" in violations[0]
    assert "0.14.0" in violations[0]
    assert ">=0.16.2" in violations[0]


def test_fail_case_nousergon_lib_tag_mismatch(requirements_in, monkeypatch):
    """Installed nousergon-lib on an older tag than requirements.in pins."""
    _patch_versions(
        monkeypatch, {"krepis": "0.16.2", "nousergon-lib": "0.120.0"}
    )
    violations = check_package_drift(requirements_in=requirements_in)
    assert len(violations) == 1
    assert "nousergon-lib" in violations[0]
    assert "0.120.0" in violations[0]
    assert "v0.124.0" in violations[0]


def test_fail_case_package_not_installed_at_all(requirements_in, monkeypatch):
    """Declared in requirements.in but absent from the venv entirely."""
    _patch_versions(monkeypatch, {"nousergon-lib": "0.124.0"})  # krepis absent
    violations = check_package_drift(requirements_in=requirements_in)
    assert len(violations) == 1
    assert "krepis" in violations[0]
    assert "NOT INSTALLED" in violations[0]


def test_package_not_declared_at_all_is_not_drift(tmp_path, monkeypatch):
    """A package absent from requirements.in entirely is skipped, not flagged."""
    path = tmp_path / "requirements.in"
    path.write_text("streamlit>=1.40\n")
    _patch_versions(monkeypatch, {})
    violations = check_package_drift(requirements_in=path)
    assert violations == []


def test_missing_requirements_in_raises(tmp_path):
    missing = tmp_path / "does-not-exist.in"
    with pytest.raises(RuntimeError, match="not found"):
        check_package_drift(requirements_in=missing)


def test_main_exits_nonzero_on_drift(requirements_in, monkeypatch, capsys):
    _patch_versions(
        monkeypatch, {"krepis": "0.14.0", "nousergon-lib": "0.124.0"}
    )
    monkeypatch.setattr(
        check_package_drift_mod, "_REQUIREMENTS_IN", requirements_in
    )
    rc = check_package_drift_mod.main()
    assert rc == 1
    captured = capsys.readouterr()
    assert "drift detected" in captured.err


def test_main_exits_zero_when_clean(requirements_in, monkeypatch, capsys):
    _patch_versions(
        monkeypatch, {"krepis": "0.16.2", "nousergon-lib": "0.124.0"}
    )
    monkeypatch.setattr(
        check_package_drift_mod, "_REQUIREMENTS_IN", requirements_in
    )
    rc = check_package_drift_mod.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out
