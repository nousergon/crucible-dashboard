"""Per-service memory must be recorded over time, and recorded honestly.

WHY (alpha-engine-config-I7804, measured 2026-08-20). The box reported
`BREACH: steady-state working set 2150 MB is 56% of RAM`, and the question
that decides what to do about it — WHICH service, and is any of them GROWING —
could not be answered, because nothing on this box records per-service memory
over time. A whole session of hand-run SSM snapshots left the growth question
open.

The one series that did exist, box-level `mem_available`, actively misled:
it showed availability falling ~300 MB over the fifteen hours after a restart,
which reads like a leak, while per-service sampling twenty minutes apart
showed no service growing at all and one Streamlit app releasing ~98 MB. A
box-level total cannot separate "leak" from "normal churn", and that is the
difference between hunting a bug and buying a bigger instance.

Three properties are pinned here because each one, if lost, would return the
series to something that looks alive while answering the wrong question:

1. **anon + swap, never `memory.current`.** A throttled unit's
   `memory.current` reports its cap. `litellm-proxy.service` measured 785 MB
   against a 786 MB `MemoryHigh` while holding 269 MB more in swap — the
   censored reading understated it by a third, which is exactly how the
   sizing argument went unresolvable.
2. **Every unit every run, absent ones as zero.** A metric that stops is
   indistinguishable from a dead publisher.
3. **Fail loud on an unreadable unit list.** Falling back to "whatever is in
   the cgroup tree" would silently change what the series means.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "infrastructure" / "emit_service_memory.sh"
BUDGET = REPO_ROOT / "infrastructure" / "systemd" / "resource-limits" / "budget.yaml"
UNIT = REPO_ROOT / "infrastructure" / "systemd" / "emit-service-memory.service"
TIMER = REPO_ROOT / "infrastructure" / "systemd" / "emit-service-memory.timer"
INSTALLER = REPO_ROOT / "infrastructure" / "install-cloudwatch-agent-config.sh"
DEPLOY = REPO_ROOT / "infrastructure" / "deploy-on-merge.sh"

_CURL_HEALTHY = """#!/bin/bash
for a in "$@"; do
  case "$a" in
    *api/token) echo "stub-token"; exit 0 ;;
    *meta-data/instance-id) echo "i-0123456789abcdef0"; exit 0 ;;
  esac
done
exit 0
"""

_AWS_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$AWS_CALL_LOG"
exit 0
"""


def _harness(tmp_path: Path, *, units: list[str], present: dict[str, tuple[int, int]]):
    """Run the real script against a fake cgroup tree and a fake budget.yaml.

    `present` maps unit -> (anon_bytes, swap_bytes); a unit in `units` but not
    in `present` has no cgroup directory at all, i.e. it is stopped.
    """
    binn = tmp_path / "bin"
    binn.mkdir()
    for name, body in (("curl", _CURL_HEALTHY), ("aws", _AWS_STUB)):
        p = binn / name
        p.write_text(body)
        p.chmod(0o755)

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    for unit, (anon, swap) in present.items():
        d = cgroup / unit
        d.mkdir()
        (d / "memory.stat").write_text(f"anon {anon}\nfile 12345\nslab 999\n")
        (d / "memory.swap.current").write_text(f"{swap}\n")

    budget = tmp_path / "budget.yaml"
    body = "ram_mb: 3839\nservices:\n"
    for unit in units:
        body += f"  - unit: {unit}\n    memory_high: 100M\n"
    # A trailing block the reader must NOT pick units out of.
    body += "timers:\n  - unit: dbus.service\n    memory_high: 50M\n"
    budget.write_text(body)

    call_log = tmp_path / "aws_calls.txt"
    call_log.touch()

    src = SCRIPT.read_text().replace(
        'CGROUP_ROOT="/sys/fs/cgroup/system.slice"', f'CGROUP_ROOT="{cgroup}"'
    )
    copy = tmp_path / "emit_service_memory.sh"
    copy.write_text(src)
    copy.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{binn}:{env['PATH']}"
    env["AWS_CALL_LOG"] = str(call_log)
    env["BUDGET_FILE"] = str(budget)

    proc = subprocess.run(
        ["bash", str(copy)], env=env, capture_output=True, text=True, timeout=60
    )
    return proc, call_log.read_text()


MB = 1048576


@pytest.mark.skipif(not SCRIPT.exists(), reason="collector not present")
class TestMeasurementIsUncensored:
    def test_value_is_anon_plus_swap(self, tmp_path):
        """The whole point: a unit parked at its cap must report its demand,
        not its cap. 700 MB resident + 269 MB swapped is a 969 MB service."""
        proc, calls = _harness(
            tmp_path,
            units=["litellm-proxy.service"],
            present={"litellm-proxy.service": (700 * MB, 269 * MB)},
        )
        assert proc.returncode == 0, proc.stderr
        assert "Value=969," in calls, (
            "expected anon+swap = 969 MiB; a value of 700 means swap was "
            f"dropped and the reading is censored.\n{calls}"
        )

    def test_swap_only_service_is_not_reported_as_zero(self, tmp_path):
        proc, calls = _harness(
            tmp_path,
            units=["a.service"],
            present={"a.service": (0, 300 * MB)},
        )
        assert proc.returncode == 0, proc.stderr
        assert "Value=300," in calls


@pytest.mark.skipif(not SCRIPT.exists(), reason="collector not present")
class TestEveryUnitEveryRun:
    def test_a_stopped_unit_publishes_zero_rather_than_nothing(self, tmp_path):
        """A series that stops is indistinguishable from a dead publisher."""
        proc, calls = _harness(
            tmp_path,
            units=["running.service", "stopped.service"],
            present={"running.service": (100 * MB, 0)},
        )
        assert proc.returncode == 0, proc.stderr
        assert "Name=Unit,Value=stopped.service" in calls, (
            "a unit with no cgroup must still publish, as zero:\n" + calls
        )
        assert "MetricName=ServiceMemoryMiB,Value=0,Unit=Megabytes,Dimensions=[{Name=Unit,Value=stopped.service}]" in calls

    def test_total_is_published_and_is_the_sum(self, tmp_path):
        proc, calls = _harness(
            tmp_path,
            units=["a.service", "b.service"],
            present={"a.service": (100 * MB, 10 * MB), "b.service": (50 * MB, 0)},
        )
        assert proc.returncode == 0, proc.stderr
        m = re.search(r"MetricName=ServiceMemoryTotalMiB,Value=(\d+),", calls)
        assert m, calls
        assert int(m.group(1)) == 160, f"expected 110+50=160, got {m.group(1)}"

    def test_units_come_from_the_services_block_only(self, tmp_path):
        """budget.yaml also carries a `timers:` block of OS plumbing. Publishing
        a 5-minute gauge of dbus's memory is paid-for noise."""
        _, calls = _harness(
            tmp_path,
            units=["real.service"],
            present={"real.service": (10 * MB, 0)},
        )
        assert "Name=Unit,Value=real.service" in calls
        assert "dbus.service" not in calls


@pytest.mark.skipif(not SCRIPT.exists(), reason="collector not present")
class TestFailsLoud:
    def test_unreadable_budget_fails_instead_of_guessing_the_unit_set(self, tmp_path):
        binn = tmp_path / "bin"
        binn.mkdir()
        for name, body in (("curl", _CURL_HEALTHY), ("aws", _AWS_STUB)):
            p = binn / name
            p.write_text(body)
            p.chmod(0o755)
        call_log = tmp_path / "aws_calls.txt"
        call_log.touch()

        copy = tmp_path / "emit_service_memory.sh"
        copy.write_text(SCRIPT.read_text())
        copy.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = f"{binn}:{env['PATH']}"
        env["AWS_CALL_LOG"] = str(call_log)
        env["BUDGET_FILE"] = str(tmp_path / "does-not-exist.yaml")

        proc = subprocess.run(
            ["bash", str(copy)], env=env, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode != 0, (
            "an unreadable unit list must fail, not fall back to the cgroup "
            f"tree — that silently changes what the series means.\n{proc.stdout}"
        )
        assert "put-metric-data" not in call_log.read_text()


@pytest.mark.skipif(not SCRIPT.exists(), reason="collector not present")
class TestReachesTheBox:
    """A collector nothing installs is indistinguishable from one that works."""

    def test_installer_installs_script_unit_and_timer(self):
        text = INSTALLER.read_text()
        assert re.search(
            r"install .*emit_service_memory\.sh.*/usr/local/bin/emit_service_memory\.sh",
            text,
        ), "the installer must place the collector on the box's PATH"
        assert "systemd/emit-service-memory.service" in text
        assert "systemd/emit-service-memory.timer" in text
        assert "systemctl enable --now emit-service-memory.timer" in text

    def test_installer_fails_loud_on_the_priming_run(self):
        """The first run is the only cheap place to see that the script cannot
        read budget.yaml — after that the timer looks green while the series
        is empty."""
        text = INSTALLER.read_text()
        block = text[text.index("priming the per-service memory series"):]
        assert "systemctl start emit-service-memory.service ||" in block
        assert "exit 1" in block[: block.index("Done. Verify")]

    def test_deploy_manifest_carries_every_input(self):
        """deploy-on-merge.sh only re-runs an installer when one of its listed
        inputs changed. An input missing from that list is a merged fix that
        never reaches the box."""
        line = next(
            l for l in DEPLOY.read_text().splitlines()
            if "install-cloudwatch-agent-config.sh|stamp" in l
        )
        for required in (
            "emit_service_memory.sh",
            "systemd/emit-service-memory.service",
            "systemd/emit-service-memory.timer",
            "systemd/resource-limits/budget.yaml",
        ):
            assert required in line, f"{required} missing from the deploy manifest"

    def test_timer_does_not_collide_with_the_oom_collector_at_boot(self):
        """Both shell out to the AWS CLI (~150M peak each) and boot is when the
        box is tightest."""
        oom = (REPO_ROOT / "infrastructure" / "systemd" / "emit-oom-metric.timer").read_text()
        svc = TIMER.read_text()
        get = lambda t: re.search(r"OnBootSec=(\d+)min", t).group(1)
        assert get(oom) != get(svc), "the two collectors must not boot in the same second"

    def test_unit_runs_as_root_and_logs_to_the_journal(self):
        text = UNIT.read_text()
        # cgroup memory.stat under system.slice is root-readable only.
        assert "User=root" in text
        assert "StandardError=journal" in text


@pytest.mark.skipif(not BUDGET.exists(), reason="budget.yaml not present")
class TestAgreesWithTheBudgetCheck:
    def test_every_budgeted_service_is_instrumented(self):
        """The collector and check_memory_budget.py must describe the same set,
        or a breach can name a service the series has no line for."""
        text = BUDGET.read_text()
        block = text[text.index("\nservices:") :]
        for key in ("\nrestart_policy:", "\nstate:", "\ntimers:"):
            if key in block:
                block = block[: block.index(key)]
        budgeted = set(re.findall(r"^\s*-\s*unit:\s*(\S+\.service)", block, re.M))
        assert budgeted, "no services parsed out of budget.yaml — reader is broken"

        # The script's own reader, run over the real file.
        out = subprocess.run(
            ["bash", "-c",
             f"awk '/^services:/{{i=1;next}} /^[a-z_]+:/{{i=0}} i' {BUDGET} "
             r"| grep -oE '^[[:space:]]*-[[:space:]]*unit:[[:space:]]*[A-Za-z0-9@_.-]+\.service' "
             r"| sed -E 's/.*unit:[[:space:]]*//' | sort -u"],
            capture_output=True, text=True, check=True,
        )
        parsed = set(out.stdout.split())
        assert parsed == budgeted, (
            "the collector's unit reader disagrees with budget.yaml's services "
            f"block.\nonly in budget: {budgeted - parsed}\n"
            f"only in reader: {parsed - budgeted}"
        )
