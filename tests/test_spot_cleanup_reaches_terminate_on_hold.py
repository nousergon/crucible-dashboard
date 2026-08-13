"""The EXIT trap must reach ``terminate-instances`` under the ``--json`` contract.

THE DEFECT THIS GUARDS AGAINST (alpha-engine-config-I7009)

``cleanup()`` in ``infrastructure/spot_train.sh`` asks
``krepis.ec2_spot relaunch-decision`` whether the spot was reclaimed by AWS.
Before krepis-PR133 (released 0.51.0) the CLI answered by EXIT CODE alone —
``0`` = relaunch, ``NO_RELAUNCH_EXIT_CODE`` (75) = hold — and a non-zero exit
was indistinguishable from "the CLI itself could not answer" (a crashed
subprocess, a missing dependency, an AWS API error). ``--json`` splits these:
the verdict is now a JSON field (``relaunch``) on stdout, and the CLI exits 0
for EVERY reached decision, hold included. A non-zero exit now means only
"the CLI could not answer" — never a verdict — and must be treated as an
explicit hold, not parsed for a verdict that isn't there.

This repo's launcher migrated to ``--json`` in this change. The guard below
replaces the old exit-code assertion (which asserted survival of a
``NO_RELAUNCH_EXIT_CODE`` "hold" answer) with two assertions against the new
contract:

  (a) the CLI answers with a well-formed ``--json`` verdict (``relaunch``:
      false) and exits 0 — cleanup must still reach ``terminate-instances``
      and must NOT record a spot-interruption-retry metric (no relaunch was
      granted).
  (b) the CLI fails outright (non-zero exit, no usable verdict) — cleanup
      must treat this as a hold (never a relaunch) and must still reach
      ``terminate-instances``, re-raising the workload's real exit status
      rather than the decision CLI's.

Both assertions fail against the pre-migration ``spot_train.sh`` (verified by
running this file against the parent commit of this PR via ``git stash``):
the old code has no ``--json`` flag and no CLI-failure branch, so a stub CLI
that exits non-zero without a ``NO_RELAUNCH_EXIT_CODE``-shaped answer is
mishandled the same way any non-zero, non-75 exit always was under the old
contract.

METHOD

The real ``cleanup`` is lifted out of the real script (brace-matched, so the
text executed is the text in the repository), installed as the EXIT trap, and
the harness then fails the way a failed SSM step does. ``aws`` is stubbed to
record every invocation. ``LIB_PYTHON`` is stubbed to answer the
``-m krepis.ec2_spot relaunch-decision ... --json`` call per-scenario, and to
delegate any ``-c ...`` invocation (the launcher's own JSON-verdict parse) to
the real ``python3`` on PATH, since that parse must run for real.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "infrastructure" / "spot_train.sh"
)

#: A CLI-internal-failure exit code — anything non-zero; --json makes 0 the
#: only "reached a verdict" status, so which non-zero value is irrelevant.
_CLI_FAILURE_EXIT_CODE = 2


def _function_text(source: str, name: str) -> str:
    """Return a shell function's full text, brace-matched."""
    marker = "\n" + name + "() {"
    assert marker in source, f"{name}() not found in {_SCRIPT.name}"
    start = source.index(marker) + 1
    depth = 0
    for idx in range(start, len(source)):
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _write_lib_python_stub(path: Path, decision_body: str) -> None:
    """A LIB_PYTHON stub that answers ``-m ... relaunch-decision`` per
    ``decision_body`` and delegates any ``-c`` invocation to real python3 —
    the launcher's own JSON-verdict parse must actually run.
    """
    _write_stub(
        path,
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-c" ]; then\n'
        "  shift\n"
        '  exec python3 -c "$@"\n'
        "fi\n"
        f"{decision_body}\n",
    )


@pytest.fixture(autouse=True)
def _requires_bash_and_python():
    if shutil.which("bash") is None:  # pragma: no cover - bash is a hard dep
        pytest.skip("bash unavailable")
    if shutil.which("python3") is None:  # pragma: no cover - python3 is a hard dep
        pytest.skip("python3 unavailable")


def _run_cleanup(tmp_path: Path, lib_python_body: str) -> tuple[str, int]:
    source = _SCRIPT.read_text()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `aws` records every invocation so the assertion is on what cleanup
    # actually CALLED, not on what it printed.
    calls = tmp_path / "aws-calls.log"
    _write_stub(
        bin_dir / "aws",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> " + str(calls) + "\nexit 0\n",
    )
    lib_python = tmp_path / "lib-python"
    _write_lib_python_stub(lib_python, lib_python_body)

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        # The condition under test — the launcher sets exactly this.
        "set -euo pipefail\n"
        "_HEARTBEAT_PID=''\n"
        "_heartbeat_stop() { :; }\n"
        f"{_function_text(source, 'cleanup')}\n"
        "AWS_REGION=us-east-1\n"
        "S3_BUCKET=test-bucket\n"
        "INSTANCE_ID=i-0000000000test0000\n"
        "S3_STAGING=s3://test-bucket/tmp/spot_train/test\n"
        "MAX_RUNTIME_SECONDS=5400\n"
        "SF_EXECUTION_TIMEOUT=''\n"
        "SPOT_ATTEMPT=1\n"
        "MAX_SPOT_ATTEMPTS=2\n"
        f"LIB_PYTHON={lib_python}\n"
        "ORIG_ARGS=()\n"
        "_ORIG_ARGS=()\n"
        "trap cleanup EXIT\n"
        # Fail the way a failed SSM step does.
        "exit 3\n"
    )
    harness.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
        timeout=60,
    )
    aws_calls = calls.read_text() if calls.exists() else ""
    return aws_calls, proc.returncode, proc.stdout, proc.stderr


def test_cleanup_terminates_on_a_well_formed_json_hold(tmp_path: Path) -> None:
    """(a) --json verdict, relaunch: false, CLI exits 0."""
    aws_calls, returncode, stdout, stderr = _run_cleanup(
        tmp_path,
        "printf '{\"relaunch\": false, \"reason\": \"other\"}'\nexit 0\n",
    )

    assert "terminate-instances" in aws_calls, (
        "cleanup never reached terminate-instances on a well-formed JSON hold "
        "verdict — the spot instance is leaked.\n"
        f"aws calls seen:\n{aws_calls or '  (none)'}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert "SpotInterruptionRetry" not in aws_calls, (
        "a relaunch=false verdict must not record a spot-interruption-retry "
        f"metric.\naws calls seen:\n{aws_calls or '  (none)'}"
    )
    assert returncode == 3, (
        f"the launcher exited {returncode}, not the workload's status 3."
    )


def test_cleanup_terminates_when_the_decision_cli_fails_outright(
    tmp_path: Path,
) -> None:
    """(b) CLI failure (non-zero exit, no verdict) must be treated as a hold,
    not parsed for a verdict — and must still reach terminate-instances."""
    aws_calls, returncode, stdout, stderr = _run_cleanup(
        tmp_path,
        "printf 'boom: could not describe instance\\n' >&2\n"
        f"exit {_CLI_FAILURE_EXIT_CODE}\n",
    )

    assert "terminate-instances" in aws_calls, (
        "cleanup never reached terminate-instances when the decision CLI "
        "failed outright — a non-zero --json exit means 'could not answer', "
        "not a verdict, and must still be held explicitly.\n"
        f"aws calls seen:\n{aws_calls or '  (none)'}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert "SpotInterruptionRetry" not in aws_calls, (
        "a CLI failure must never be treated as a relaunch verdict.\n"
        f"aws calls seen:\n{aws_calls or '  (none)'}"
    )
    assert returncode == 3, (
        f"the launcher exited {returncode}, not the workload's status 3. "
        "A CLI failure must never override the real workload exit status."
    )
