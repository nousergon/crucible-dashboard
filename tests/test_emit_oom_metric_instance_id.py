"""emit_oom_metric.sh must never publish OOMKills without a real instance id.

MEASURED 2026-08-20: `emit-oom-metric.timer` on the dashboard box failed at
10:21 and 14:01 UTC with

    Error parsing parameter '--metric-data': Expected: ',', received: ']'
     MetricName=OOMKills,Value=0,Unit=Count,Dimensions=[{Name=InstanceId,Value=}]

The dimension was EMPTY, not the `"unknown"` the old `|| echo "unknown"`
fallback promised, because `curl -s` without `-f` exits 0 on an HTTP error.
When the IMDSv2 token PUT timed out, the follow-up GET got a 401 with an empty
body and curl still reported success — so the fallback guarded a case that does
not occur and missed the one that does.

These run the real script with stubbed `curl` / `aws` / `journalctl` on PATH,
because the failure is in control flow that source-text assertions cannot
reach: `curl` exiting 0 with empty output is precisely what the old code could
not distinguish from success.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "infrastructure" / "emit_oom_metric.sh"

_JOURNALCTL_STUB = """#!/bin/bash
# --show-cursor is the cursor-advance call; everything else is a log read.
for a in "$@"; do
  if [ "$a" = "--show-cursor" ]; then echo "-- cursor: s=stub;i=1"; exit 0; fi
done
exit 0
"""

_AWS_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$AWS_CALL_LOG"
exit 0
"""


def _stub_dir(tmp_path: Path, curl_body: str) -> Path:
    """A PATH shim directory: our stubs first, then the real system tools."""
    d = tmp_path / "bin"
    d.mkdir()
    for name, body in (
        ("journalctl", _JOURNALCTL_STUB),
        ("aws", _AWS_STUB),
        ("curl", curl_body),
    ):
        p = d / name
        p.write_text(body)
        p.chmod(0o755)
    return d


def _run(tmp_path: Path, curl_body: str):
    stubs = _stub_dir(tmp_path, curl_body)
    call_log = tmp_path / "aws_calls.txt"
    call_log.touch()
    state = tmp_path / "state"
    state.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    env["AWS_CALL_LOG"] = str(call_log)

    # STATE_DIR is hardcoded to /var/lib/alpha-engine; redirect it so the test
    # never touches a real cursor. `sed` on a copy keeps the script itself the
    # thing under test.
    src = SCRIPT.read_text().replace(
        'STATE_DIR="/var/lib/alpha-engine"', f'STATE_DIR="{state}"'
    )
    copy = tmp_path / "emit_oom_metric.sh"
    copy.write_text(src)
    copy.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(copy)], env=env, capture_output=True, text=True, timeout=60
    )
    return proc, call_log.read_text()


# `curl -sf` against a 401 exits 22 and prints nothing. The OLD script used
# `curl -s`, which exits 0 and prints nothing — this stub reproduces the
# unfixed upstream behaviour (success + empty body) so the test fails loudly
# if the `-f` / shape-check pair is ever removed.
_CURL_EMPTY_BUT_SUCCESSFUL = """#!/bin/bash
exit 0
"""

_CURL_HEALTHY = """#!/bin/bash
for a in "$@"; do
  case "$a" in
    *api/token) echo "AQAAA-stub-token"; exit 0 ;;
    *meta-data/instance-id) echo "i-0123456789abcdef0"; exit 0 ;;
  esac
done
exit 0
"""


@pytest.mark.skipif(not SCRIPT.exists(), reason="script not present")
class TestInstanceIdDimension:
    def test_empty_imds_response_fails_instead_of_publishing(self, tmp_path):
        """The observed 2026-08-20 failure, reproduced: IMDS answers with an
        empty body and exit 0. The script must exit non-zero and must NOT call
        put-metric-data at all."""
        proc, aws_calls = _run(tmp_path, _CURL_EMPTY_BUT_SUCCESSFUL)

        assert proc.returncode != 0, (
            "an unresolvable instance id must fail the unit, not publish "
            f"a malformed dimension.\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        assert "put-metric-data" not in aws_calls, (
            "put-metric-data was called without a resolved instance id; the "
            f"aws call log was:\n{aws_calls}"
        )
        assert "instance id" in proc.stderr.lower(), (
            "the failure message must name the instance id as the cause, "
            f"got: {proc.stderr}"
        )

    def test_never_publishes_a_placeholder_dimension(self, tmp_path):
        """`unknown` is worse than failing: it publishes a real datapoint under
        a fake dimension, forking the OOMKills series into a stream no alarm
        watches."""
        _, aws_calls = _run(tmp_path, _CURL_EMPTY_BUT_SUCCESSFUL)
        assert "unknown" not in aws_calls
        assert "InstanceId,Value=}" not in aws_calls
        assert "InstanceId,Value=]" not in aws_calls

    def test_healthy_imds_publishes_the_real_instance_id(self, tmp_path):
        """The guard must not have broken the working path."""
        proc, aws_calls = _run(tmp_path, _CURL_HEALTHY)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        assert "put-metric-data" in aws_calls
        assert "Name=InstanceId,Value=i-0123456789abcdef0" in aws_calls
        assert "MetricName=OOMKills" in aws_calls


@pytest.mark.skipif(not SCRIPT.exists(), reason="script not present")
class TestSourceContract:
    def test_imds_curls_use_fail_on_http_error(self):
        """`-f` is the whole reason the shape check ever sees a failure: without
        it an IMDS 401 is indistinguishable from a success with no body."""
        for line in SCRIPT.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "169.254.169.254" in stripped or "curl" in stripped:
                if "curl" in stripped:
                    assert "-sf" in stripped or "-f " in stripped, (
                        "IMDS curl must fail on HTTP errors, else a 401 reads "
                        f"as an empty success: {stripped}"
                    )
