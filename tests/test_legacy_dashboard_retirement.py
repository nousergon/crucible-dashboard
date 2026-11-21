"""Retire only the legacy Streamlit runtime and retain the deployed app gates."""

from pathlib import Path
import re
import shutil
import sys
import subprocess

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
DEPLOY = INFRA / "deploy-on-merge.sh"
BOOT = INFRA / "boot-pull.sh"


def _function(script: Path, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", script.read_text(), re.M | re.S)
    assert match, f"{name} missing from {script.name}"
    return match.group()


@pytest.mark.parametrize("script", [DEPLOY, BOOT])
def test_deploy_paths_do_not_activate_or_probe_legacy_streamlit(script):
    source = script.read_text()
    assert not re.search(r"systemctl (?:enable|start|restart) dashboard(?:\.service)?\b(?!-)", source)
    assert "http://localhost:8501" not in source
    assert "nous-ergon-live" in source


def test_legacy_source_remains_and_dashboard_deploy_leaves_ingress_to_box_config():
    assert (INFRA.parent / "app.py").is_file()
    assert not (INFRA / "nginx.conf").exists()
    assert not (INFRA.parent / "live" / "infrastructure" / "nginx.conf").exists()
    for script in (DEPLOY, BOOT):
        assert "/etc/nginx/conf.d/" not in script.read_text()


def test_retirement_disables_and_masks_only_legacy_unit(tmp_path):
    unit = tmp_path / "dashboard.service"
    unit.write_text("[Service]\nExecStart=/bin/false\n")
    dropin = tmp_path / "dashboard.service.d" / "99-resource-limits.conf"
    dropin.parent.mkdir()
    dropin.touch()
    other = tmp_path / "crucible-dash-api.service"
    other.touch()
    function = _function(DEPLOY, "retire_legacy_dashboard").replace(
        "/etc/systemd/system", str(tmp_path)
    )
    shell = r"""
set -eu
LOG=/dev/null
log() { :; }
systemctl() {
    case "$*" in
        "disable --now dashboard.service") echo "$*";;
        "mask dashboard.service") echo "$*"; ln -s /dev/null "$UNIT_FILE" ;;
        "show -p ActiveState --value dashboard.service") echo inactive;;
        *) echo "unexpected systemctl $*" >&2; return 1;;
    esac
}
"""
    result = subprocess.run(
        ["bash", "-c", f'{shell}\n{function}\nUNIT_FILE="{unit}"\n'
         "retire_legacy_dashboard\nretire_legacy_dashboard\n"],
        check=True, text=True, capture_output=True,
    )
    assert result.stdout.splitlines() == [
        "disable --now dashboard.service", "mask dashboard.service",
    ]
    assert unit.readlink() == Path("/dev/null")
    assert not dropin.exists()
    assert other.is_file()


@pytest.mark.parametrize("bad", [None, "live", "dash-api", "dash-web"])
def test_deploy_rechecks_all_remaining_app_endpoints(bad, tmp_path):
    (tmp_path / "dash-web").mkdir()
    function = _function(DEPLOY, "check_deployed_apps")
    shell = r"""
set -u
LIVE_URL=live
DASH_API_URL=dash-api
DASH_WEB_URL=dash-web
wait_for_health() {
    echo "$1"
    [[ "$1" != "$BROKEN" ]]
}
"""
    result = subprocess.run(
        ["bash", "-c", f'{shell}\n{function}\nREPO_DIR="{tmp_path}"\n'
         f'BROKEN="{bad or ""}"\ncheck_deployed_apps " [post-revert]"'],
        text=True, capture_output=True,
    )
    assert result.stdout.splitlines() == ["live", "dash-api", "dash-web"]
    assert result.returncode == (1 if bad else 0)


@pytest.mark.parametrize(
    ("paused", "problem", "observe_rc", "restart", "failures"),
    [
        (False, "", 0, True, 0),
        (True, "", 0, False, 0),
        (True, "paused service running", 0, False, 1),
        (True, "paused port still listening", 0, False, 1),
        (True, "", 1, False, 1),
    ],
)
def test_boot_pull_restarts_only_undeclared_services(
    paused, problem, observe_rc, restart, failures
):
    helper = _function(BOOT, "restart_managed_service")
    shell = r"""
LOG=/dev/null
log() { :; }
workload_is_paused() { [ "$PAUSED" = yes ]; }
workload_pause_problems() { printf '%s' "$PROBLEM"; return "$OBSERVE_RC"; }
sudo() { printf 'sudo %s\n' "$*"; }
PULL_FAILURES=0
FAILED_REPOS=()
RESTARTED_SERVICES=()
"""
    result = subprocess.run(
        ["bash", "-c", f'{shell}\n{helper}\nPAUSED={"yes" if paused else "no"}\n'
         f'PROBLEM="{problem}"\nOBSERVE_RC={observe_rc}\n'
         'restart_managed_service signal.service\n'
         'echo "restarted=${RESTARTED_SERVICES[*]} failures=$PULL_FAILURES"'],
        text=True, capture_output=True, check=True,
    )
    expected = ["sudo systemctl restart signal.service"] if restart else []
    expected.append(f"restarted={'signal.service' if restart else ''} failures={failures}")
    assert result.stdout.splitlines() == expected


def test_boot_pull_keeps_both_active_apps_configured():
    source = BOOT.read_text()
    assert "fetch_config_from_ssm /alpha-engine/dashboard/config.yaml" in source
    assert "fetch_config_from_ssm /alpha-engine/dashboard/live-config.yaml" in source
    assert 'if [ "$API_CONFIG_CHANGED" -eq 1 ]; then' in source
    assert "restart_managed_service crucible-dash-api.service" in source
    assert "restart_managed_service nous-ergon-live.service" in source



@pytest.mark.parametrize("state", ["installed", "absent", "unguarded"])
def test_installed_memory_watchdog_reads_its_bundle_after_app_rollback(tmp_path, state):
    checkout = tmp_path / "checkout" / "infrastructure"
    budget = checkout / "systemd" / "resource-limits" / "budget.yaml"
    budget.parent.mkdir(parents=True)
    budget.write_text(
        "ram_mb: 2000\nreserve_fraction: 0.15\nmax_overcommit_ratio: 2.0\n"
        "max_steady_state_fraction: 0.60\nservices:\n"
        "  - unit: parked.service\n    memory_high: 100M\n    memory_max: 150M\n"
    )
    for name in ("workload_pauses.py", "systemd_memory.py", "check_memory_budget.py"):
        shutil.copy(INFRA / name, checkout / name)
    bundle = tmp_path / "installed"
    installer = _function(INFRA / "install-box-health.sh", "install_memory_budget_bundle")
    subprocess.run(
        ["bash", "-c", installer + '\nREPO_INFRA="$1"\nMEMORY_BUNDLE_ROOT="$2"\n'
         "install_memory_budget_bundle", "test", str(checkout), str(bundle)],
        check=True, text=True, capture_output=True,
    )

    # The revert restores the old application tree without reinstalling host
    # controls. That tree cannot validate the currently installed host units.
    (checkout / "dashboard.service").touch()
    (checkout / "workload_pauses.py").unlink()
    (checkout / "systemd_memory.py").unlink()
    (checkout / "check_memory_budget.py").write_text("raise SystemExit(99)\n")
    budget.write_text(budget.read_text().replace("parked.service", "dashboard.service"))

    pause_file = tmp_path / "pauses.tsv"
    pause_file.write_text("parked.service\t-\ttest-ref\ttest pause\n")
    cgroups = tmp_path / "cgroups"
    cgroups.mkdir()
    watchdog = (INFRA / "box_health.sh").read_text()
    target = re.search(r'^BUDGET_CHECK="(.+)"$', watchdog, re.M)
    assert target
    if state == "absent":
        guard = tmp_path / "parked.service.d" / "90-workload-pause.conf"
        guard.parent.mkdir()
        guard.write_text(f"[Unit]\nConditionPathExists=!{guard}\n")
    checker = target[1].replace("$MEMORY_BUNDLE_ROOT", str(bundle))
    observation = """
import importlib
import os
import sys
import types
from pathlib import Path

checker, fixture = map(Path, sys.argv[1:3])
state = sys.argv[3]
package = types.ModuleType("infrastructure")
package.__path__ = [str(checker.parent)]
sys.modules[package.__name__] = package
budget = importlib.import_module("infrastructure." + checker.stem)
pauses = importlib.import_module("infrastructure.workload_pauses")
memory = importlib.import_module("infrastructure.systemd_memory")

unit = "parked.service"
properties = {"ActiveState": "inactive", "ControlGroup": ""}
if state == "installed":
    properties.update(LoadState="loaded", UnitFileState="disabled", MemoryMax="150M", MemoryHigh="100M")
else:
    properties.update(LoadState="not-found", UnitFileState="")
    if state == "unguarded":
        properties.update(MemoryMax="infinity", MemoryHigh="infinity", ActiveEnterTimestampMonotonic="0")
budget._CGROUP_ROOT = fixture / "cgroups"
(budget._CGROUP_ROOT / "cgroup.procs").write_text("")
memory.live_services = lambda expected: {unit: memory.Service(
    unit, frozenset({unit}), properties["LoadState"], "inactive", "simple", "",
    properties["UnitFileState"], properties.get("MemoryMax", ""), 0,
)}
memory.listener_ports = lambda: set()
budget._DROPIN_ROOT = fixture / "dropins"
budget._PEAK_MARKS = fixture / "peaks.json"
budget.systemd_show = lambda name, prop: properties[prop] if unit == name else None
budget.ram_mb_from_proc = lambda: 2000
memory.verify_pause_guard = lambda unit: pauses.verify_pause_guard(
    unit, root=fixture, expected_uid=os.getuid()
)
budget.load_workload_pauses = lambda: pauses.load_workload_pauses(
    fixture / "pauses.tsv", expected_uid=os.getuid(), required=True
)
sys.argv = [str(checker), "--installed", "--quiet"]
raise SystemExit(budget.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", observation, checker, str(tmp_path), state],
        cwd=checkout.parent, text=True, capture_output=True,
    )
    assert result.returncode == (1 if state == "unguarded" else 0), result.stdout + result.stderr
    if state == "unguarded":
        assert "pause guard unreadable" in result.stderr
