"""Wiring pins for substrate-health-daily (config#2954).

Static-source guards (no live box/systemd needed) for the three defects
found in production: bare ``python`` (AL2023 has no bare python symlink on
PATH — and this venv's own ``bin/python`` symlink has gone missing at least
once), a log path the service's User= can't write
(``/var/log/*.log`` is root-owned), and a failed/never-finalized nightly
run being invisible (no OnFailure= alerting path existed at all).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INFRA = _REPO_ROOT / "infrastructure"
_SCRIPT = _INFRA / "substrate_health_check_daily.sh"
_SERVICE = _INFRA / "systemd" / "substrate-health-daily.service"
_ALERT_TEMPLATE = _INFRA / "systemd" / "alert-on-failure@.service"
_ALERT_SCRIPT = _INFRA / "alert_on_failure.sh"
_INSTALLER = _INFRA / "install-substrate-health-daily.sh"


class TestScriptInterpreter:
    def test_no_bare_python_invocation(self):
        src = _SCRIPT.read_text()
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # A bare `python ` (not python3, not $PYTHON_BIN, not inside a
            # longer word/path) invoking the interpreter directly.
            assert not stripped.startswith("python "), (
                f"{_SCRIPT.name}:{lineno} invokes bare `python` — AL2023 has "
                f"no bare python symlink on PATH: {stripped!r}"
            )

    def test_uses_absolute_venv_interpreter(self):
        src = _SCRIPT.read_text()
        assert "/home/ec2-user/alpha-engine-dashboard/.venv/bin/python" in src


class TestLogPath:
    def test_does_not_write_directly_under_var_log(self):
        src = _SCRIPT.read_text()
        # The old defect: `tee /var/log/substrate-health-check-daily.log` —
        # /var/log/ itself is root-owned, not writable by User=ec2-user.
        # LogsDirectory=-backed subdirectories are fine (checked below).
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "/var/log/substrate-health-daily/" in line:
                continue  # the LogsDirectory=-backed path
            assert "/var/log/" not in line, (
                f"{_SCRIPT.name}:{lineno} writes directly under /var/log/ "
                f"(not the LogsDirectory=-backed subdir): {line.strip()!r}"
            )

    def test_service_declares_logs_directory(self):
        src = _SERVICE.read_text()
        assert "LogsDirectory=substrate-health-daily" in src

    def test_script_log_path_matches_logs_directory(self):
        script_src = _SCRIPT.read_text()
        service_src = _SERVICE.read_text()
        logs_dir = "substrate-health-daily"
        assert f"/var/log/{logs_dir}/" in script_src
        assert f"LogsDirectory={logs_dir}" in service_src


class TestFailureAlerting:
    def test_service_sets_onfailure(self):
        src = _SERVICE.read_text()
        assert "OnFailure=alert-on-failure@%n.service" in src

    def test_alert_template_exists_and_invokes_handler_script(self):
        assert _ALERT_TEMPLATE.exists()
        src = _ALERT_TEMPLATE.read_text()
        assert "ExecStart=/home/ec2-user/alpha-engine-dashboard/infrastructure/alert_on_failure.sh %i" in src

    def test_alert_script_exists_and_publishes_via_krepis(self):
        assert _ALERT_SCRIPT.exists()
        src = _ALERT_SCRIPT.read_text()
        # config#1649: real krepis module, never the nousergon_lib shim.
        assert "-m krepis.alerts publish" in src
        assert "-m nousergon_lib.alerts" not in src

    def test_alert_script_dedups_per_unit_per_day(self):
        src = _ALERT_SCRIPT.read_text()
        assert "--dedup-key" in src
        assert '"$UNIT"' in src or "${UNIT}" in src

    def test_installer_installs_alert_template(self):
        src = _INSTALLER.read_text()
        assert "alert-on-failure@.service" in src
        assert "alert_on_failure.sh" in src
