"""Tests for infrastructure/check_lib_ref_parity.sh.

The guard closes a defect this repo has now shipped three times: the
nousergon-lib VCS ref moved forward in ``requirements.txt`` (the lock, which
decides what is INSTALLED) while ``requirements.in`` — the file
``infrastructure/check_package_drift.py`` reads on the box — stayed behind.
The box's deploy preflight then compares the .in tag against the installed
version, finds them different, and refuses every deploy, with CI green
throughout because CI never installs and never compared the two refs.

  #739  2026-08-20  three merges landed on main and none reached the box
  #775  2026-08-22  every deploy failed until 2026-08-25
  #813  2026-08-31  deploy.yml FAILED 8 of 8 runs, 2026-09-02..09-06

``check_lock_reproducible.sh`` compares that line by PRESENCE and EXTRAS only,
never by ref, so it could not see any of them. These tests drive the parity
guard directly with scratch requirements files, so they need neither ``uv``
nor a network resolve.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "infrastructure" / "check_lib_ref_parity.sh"

_IN_TEMPLATE = """\
streamlit>=1.62.0
krepis[openai]>=0.59.39
nousergon-lib[flow-doctor,github_app] @ git+https://github.com/nousergon/nousergon-lib@{ref}
"""

_TXT_TEMPLATE = """\
streamlit==1.62.0
    # via -r requirements.in
nousergon-lib[flow-doctor, github-app] @ git+https://github.com/nousergon/nousergon-lib@{ref}
    # via -r requirements.in
"""


def _run(tmp_path, in_ref: str, txt_ref: str) -> subprocess.CompletedProcess:
    req_in = tmp_path / "requirements.in"
    req_txt = tmp_path / "requirements.txt"
    req_in.write_text(_IN_TEMPLATE.format(ref=in_ref))
    req_txt.write_text(_TXT_TEMPLATE.format(ref=txt_ref))
    return subprocess.run(
        ["bash", str(GUARD), str(req_in), str(req_txt)],
        capture_output=True,
        text=True,
    )


def test_guard_is_executable_and_wired_into_the_lock_check():
    assert GUARD.is_file(), "the parity guard must exist"
    caller = (REPO_ROOT / "infrastructure" / "check_lock_reproducible.sh").read_text()
    assert "check_lib_ref_parity.sh" in caller, (
        "check_lock_reproducible.sh (the job CI actually runs, "
        "`lockfile-reproducible`) must invoke the parity guard — a guard "
        "nothing calls detects nothing."
    )


def test_equal_tag_refs_pass(tmp_path):
    result = _run(tmp_path, "v0.124.104", "v0.124.104")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_mismatched_tag_refs_fail_and_name_both_refs(tmp_path):
    """The exact #813 shape: lock ahead of the .in by two patch releases."""
    result = _run(tmp_path, "v0.124.102", "v0.124.104")
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "v0.124.102" in combined, "the failure must name the requirements.in ref"
    assert "v0.124.104" in combined, "the failure must name the requirements.txt ref"
    assert "repoint requirements.in" in combined and "recompile" in combined, (
        "the failure must name the fix for whichever half is stale"
    )


def test_equal_raw_sha_refs_pass(tmp_path):
    """A compile may legitimately resolve a tag to the SHA it points at.

    The guard forbids the two halves naming DIFFERENT code, never a
    particular ref FORM — so a raw SHA on both sides passes.
    """
    sha = "68d643424ddef7f38648d3919842b1bbe7aa731a"
    result = _run(tmp_path, sha, sha)
    assert result.returncode == 0, result.stdout + result.stderr


def test_mismatched_raw_sha_refs_fail(tmp_path):
    a = "68d643424ddef7f38648d3919842b1bbe7aa731a"
    b = "0123456789abcdef0123456789abcdef01234567"
    result = _run(tmp_path, a, b)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert a in combined and b in combined


def test_tag_on_one_side_and_sha_on_the_other_fails(tmp_path):
    """Not a form judgement — these are two different strings naming code.

    A tag and a SHA cannot be proven equal without a network resolve, and the
    box's check_package_drift compares the .in tag textually, so this must be
    loud rather than assumed-equivalent.
    """
    result = _run(tmp_path, "v0.124.104", "68d643424ddef7f38648d3919842b1bbe7aa731a")
    assert result.returncode == 1


def test_missing_pin_fails_rather_than_passing_vacuously(tmp_path):
    req_in = tmp_path / "requirements.in"
    req_txt = tmp_path / "requirements.txt"
    req_in.write_text("streamlit>=1.62.0\n")
    req_txt.write_text("streamlit==1.62.0\n")
    result = subprocess.run(
        ["bash", str(GUARD), str(req_in), str(req_txt)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "no nousergon-lib git pin" in result.stdout + result.stderr


def test_missing_file_fails(tmp_path):
    result = subprocess.run(
        ["bash", str(GUARD), str(tmp_path / "nope.in"), str(tmp_path / "nope.txt")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "not found" in result.stdout + result.stderr


def test_committed_files_agree():
    """The live check, against the files this repo actually ships."""
    result = subprocess.run(
        ["bash", str(GUARD)], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _committed_ref(fname: str) -> str:
    lines = [
        ln
        for ln in (REPO_ROOT / fname).read_text().splitlines()
        if ln.startswith("nousergon-lib") and "git+" in ln
    ]
    assert len(lines) == 1, f"{fname} must carry exactly one nousergon-lib git pin"
    return lines[0].split("#", 1)[0].rstrip().rsplit("@", 1)[-1]


def test_committed_refs_are_equal():
    """Belt-and-braces in pure Python, independent of the shell guard.

    Asserts EQUALITY, not a literal ref — the exact tag is asserted in exactly
    one place, tests/test_flow_doctor_wiring.py::
    test_requirements_in_pins_lib_to_stable_tag, and a second copy of it here
    would just be another half to leave behind.
    """
    assert _committed_ref("requirements.in") == _committed_ref("requirements.txt")
