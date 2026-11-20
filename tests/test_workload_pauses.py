"""The private host pause declaration is data, with an exact unit identity."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.box_health_helpers import function_source


INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
UNIT = "example-web.service"
PROVENANCE = "example-I123"
REASON = "Paused until further notice"
RECORD = f"{UNIT}\t3004\t{PROVENANCE}\t{REASON}\n"


def _load(path):
    from infrastructure.workload_pauses import load_workload_pauses

    return load_workload_pauses(path, expected_uid=os.getuid())


def test_missing_registry_leaves_existing_workloads_in_service(tmp_path):
    assert _load(tmp_path / "missing") == {}


def test_registry_preserves_exact_unit_port_and_provenance(tmp_path):
    path = tmp_path / "pauses.tsv"
    path.write_text(f"# Optional header\n{RECORD}example-job.timer\t-\t{PROVENANCE}\t{REASON}\n")
    pauses = _load(path)
    assert set(pauses) == {UNIT, "example-job.timer"}
    assert pauses[UNIT] == (3004, PROVENANCE, REASON)
    assert pauses["example-job.timer"] == (None, PROVENANCE, REASON)


@pytest.mark.parametrize(
    "data",
    [
        RECORD + RECORD,
        RECORD + f"other.service\t3004\t{PROVENANCE}\t{REASON}\n",
        f"*.service\t-\t{PROVENANCE}\t{REASON}\n",
        f"fixture@.service\t-\t{PROVENANCE}\t{REASON}\n",
        f"{UNIT}\t65536\t{PROVENANCE}\t{REASON}\n",
        f"fixture.timer\t3004\t{PROVENANCE}\t{REASON}\n",
        f"{UNIT}\t-\t\t{REASON}\n",
        f"{UNIT}\t-\t{PROVENANCE}\t   \n",
    ],
)
def test_registry_rejects_malformed_or_ambiguous_entries(tmp_path, data):
    path = tmp_path / "pauses.tsv"
    path.write_text(data)
    with pytest.raises(ValueError, match="line"):
        _load(path)


def test_registry_rejects_insecure_owner_mode_and_symlink(tmp_path):
    from infrastructure.workload_pauses import load_workload_pauses

    path = tmp_path / "pauses.tsv"
    path.write_text(RECORD)
    with pytest.raises(ValueError, match="owner"):
        load_workload_pauses(path, expected_uid=os.getuid() + 1)
    path.chmod(0o662)
    with pytest.raises(ValueError, match="writ"):
        _load(path)

    alias = tmp_path / "alias.tsv"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        _load(alias)


def test_shell_helper_loads_exact_units_atomically():
    helper = INFRA / "lib" / "workload-pause.sh"
    parser_payload = f"{RECORD}example-job.timer\t-\t{PROVENANCE}\t{REASON}\n"
    bash = "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else "bash"
    script = """
        set -euo pipefail
        source "$1"
        records=$2
        workload_pause_records() { printf '%s' "$records"; }
        load_workload_pauses
        workload_is_paused example-web.service
        workload_is_paused example-job.timer
        ! workload_is_paused sibling-example-web.service
        [[ ${WORKLOAD_PAUSE_PORTS[example-web.service]} == 3004 ]]
        workload_pause_records() { return 1; }
        ! load_workload_pauses
        ! workload_is_paused example-web.service
    """
    result = subprocess.run(
        [bash, "-c", script, "_", str(helper), parser_payload], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr



@pytest.mark.parametrize("mode, status", [([], 0), (["--unit", UNIT], 1), (["--validate"], 2)])
def test_cli_distinguishes_optional_policy_from_required_policy(tmp_path, mode, status):
    result = subprocess.run(
        [sys.executable, str(INFRA / "workload_pauses.py"),
         "--file", str(tmp_path / "missing"), *mode], capture_output=True, text=True,
    )
    assert result.returncode == status, result.stderr
    assert result.stdout == ""


def test_box_health_detects_paused_unit_and_port_drift():
    bash = "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else "bash"
    script = r"""
        set -uo pipefail
        source "$1"
        declare -A SERVICE_PORT=([example-web.service]=3004 [sibling.service]=3005)
        PORTS=(3004 3005 8501)
        WORKLOAD_PAUSE_PORTS=([example-web.service]=3004 [example-job.timer]=-)
        systemctl() {
            case "$*" in
                *example-web.service*) printf '%s\n' "ActiveState=$fake_active" LoadState=loaded UnitFileState=disabled ;;
                *example-job.timer*) printf '%s\n' ActiveState=inactive LoadState=loaded "UnitFileState=$timer_state" ;;
                *dashboard.service*) printf '%s\n' ActiveState=inactive LoadState=not-found UnitFileState= ;;
            esac
        }
        fake_active=inactive timer_state=disabled
        workload_pause_unit_problems
        workload_port_problems 'LISTEN 0 256 127.0.0.1:3005 0.0.0.0:*'
        echo '--- drift ---'
        fake_active=active timer_state=enabled
        workload_pause_unit_problems
        workload_port_problems $'LISTEN 0 256 127.0.0.1:3004 0.0.0.0:*\nLISTEN 0 256 127.0.0.1:8501 0.0.0.0:*'
        echo '--- wrong declaration ---'
        WORKLOAD_PAUSE_PORTS=([example-web.service]=3005)
        workload_port_problems 'LISTEN 0 256 127.0.0.1:3004 0.0.0.0:*'
    """
    funcs = "\n".join(function_source(name) for name in (
        "workload_pause_unit_problems", "workload_port_problems",
    ))
    result = subprocess.run(
        [bash, "-c", funcs + "\n" + script, "_", str(INFRA / "lib" / "workload-pause.sh")],
        capture_output=True, text=True, check=True,
    )
    assert result.stderr == ""
    clean, remainder = result.stdout.split("--- drift ---\n")
    drift, mismatch = remainder.split("--- wrong declaration ---\n")
    assert clean == ""
    assert "paused workload unit drift: example-web.service" in drift
    assert "paused workload unit drift: example-job.timer" in drift
    assert "paused workload port listening: example-web.service (3004)" in drift
    assert "retired workload port listening: dashboard.service (8501)" in drift
    assert "port not listening: 3005" in drift
    assert "paused workload port conflicts" in mismatch
    assert "port not listening: 3005" in mismatch
