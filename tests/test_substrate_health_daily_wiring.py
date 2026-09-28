"""Wiring pins for substrate-health-daily (config#2954).

Static-source guards (no live box/systemd needed) for the three defects
found in production: bare ``python`` (AL2023 has no bare python symlink on
PATH — and this venv's own ``bin/python`` symlink has gone missing at least
once), a log path the service's User= can't write
(``/var/log/*.log`` is root-owned), and a failed/never-finalized nightly
run being invisible (no OnFailure= alerting path existed at all).
"""

from __future__ import annotations

import re
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


_TIMER = _INFRA / "systemd" / "substrate-health-daily.timer"


class TestOrderedAfterThePostclosePipeline:
    """alpha-engine-config-I11581: the check runs after the postclose pipeline
    FINISHES, not at a clock time that used to be after it.

    The daily transparency rows (`pnl_attribution` from EOD reconcile,
    `trade_execution_lineage`, `risk_events`, `data_quality`) are written by
    `ne-postclose-trading-pipeline`. After the decoupled data cutover
    (nousergon-data#1930) that pipeline waits for `ne-data-collection-eod`
    (18:15 ET, worst case 20:55 ET) before reconcile, so it ends around
    20:30 ET instead of ~17:45 ET. At the old fixed 22:30 UTC (18:30 EDT /
    17:30 EST) every cycle's check would grade the PREVIOUS day's rows.
    """

    _MARKER = "_sf_completion/ne-postclose-trading-pipeline/"

    def _code_lines(self) -> list[str]:
        return [
            line.strip()
            for line in _SCRIPT.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_the_script_waits_on_the_postclose_completion_marker(self):
        lines = self._code_lines()
        marker_at = next((i for i, line in enumerate(lines) if self._MARKER in line), None)
        check_at = next(i for i, line in enumerate(lines) if "-m nousergon_lib.transparency" in line)
        assert marker_at is not None, "the script never reads the postclose completion marker"
        assert marker_at < check_at, "the marker is read after the check has already run"

    def test_the_run_date_is_the_new_york_date_not_the_box_utc_date(self):
        """The marker is keyed by the pipeline's `run_date`, a New York
        trading date; the box runs in UTC."""
        src = _SCRIPT.read_text()
        assert 'RUN_DATE="$(TZ=America/New_York date +%F)"' in src
        assert self._MARKER + "${RUN_DATE}.json" in src

    def test_the_wait_is_bounded_and_the_service_outlives_it(self):
        src = _SCRIPT.read_text()
        match = re.search(r'WAIT_UNTIL_ET="\$\{SUBSTRATE_HEALTH_WAIT_UNTIL_ET:-(\d\d):(\d\d)\}"', src)
        assert match, "no bounded wait deadline in the script"
        wait_until = int(match.group(1)) * 60 + int(match.group(2))

        timer = _TIMER.read_text()
        fire = re.search(r"^OnCalendar=Mon\.\.Fri \*-\*-\* (\d\d):(\d\d):00 America/New_York$", timer, re.M)
        assert fire, "the timer is not declared in America/New_York"
        fires = int(fire.group(1)) * 60 + int(fire.group(2))
        assert fires < wait_until < 24 * 60, "the wait must end after the fire and before New York midnight"

        timeout = re.search(r"^TimeoutStartSec=(\d+)$", _SERVICE.read_text(), re.M)
        assert timeout, "the service declares no TimeoutStartSec"
        # The whole wait, plus the original 300 s budget for the check itself.
        assert int(timeout.group(1)) >= (wait_until - fires) * 60 + 300
