"""Retire only the legacy Streamlit runtime and retain the deployed app gates."""

from pathlib import Path
import re
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


def test_legacy_hostname_redirects_to_console():
    nginx = (INFRA / "nginx.conf").read_text()
    legacy = nginx.split("server_name dashboard.nousergon.ai;", 1)[1].split("\nserver {", 1)[0]
    assert "return 302 https://console.nousergon.ai/;" in legacy
    assert "proxy_pass" not in legacy
    assert "127.0.0.1:8502" in nginx
    assert (INFRA.parent / "app.py").is_file()


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
