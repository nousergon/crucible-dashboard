"""Wiring pins for infrastructure/substrate_health_check.sh (alpha-engine-
config-I7047 deliverable 1).

Static-source guards (no live box needed) for the SF-invoked weekly-cadence
substrate health check, extracted from the WeeklySubstrateHealthCheck SF
state's previously-inlined command list. This script is invoked through
``krepis.ssm_log_capture`` (nousergon-data infrastructure/step_function.json)
rather than via its own trap/log wrapper, so — unlike
substrate_health_check_daily.sh's systemd-timer sibling — it must NOT ship
its own log or S3 upload; the SF's krepis wrapper owns that.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "infrastructure" / "substrate_health_check.sh"


def _executable_lines():
    """Yield (lineno, line) for non-comment, non-blank lines of the script.

    Keeps the guards below from tripping on the script's own header comment,
    which documents the anti-patterns by name.
    """
    for lineno, line in enumerate(_SCRIPT.read_text().splitlines(), start=1):
        if line.strip().startswith("#") or not line.strip():
            continue
        yield lineno, line


class TestScriptExists:
    def test_script_exists_and_executable(self):
        assert _SCRIPT.exists()
        assert _SCRIPT.stat().st_mode & 0o111, f"{_SCRIPT} is not executable"


class TestScriptInterpreter:
    def test_no_bare_python_invocation(self):
        src = _SCRIPT.read_text()
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("python "), (
                f"{_SCRIPT.name}:{lineno} invokes bare `python` — AL2023 has "
                f"no bare python symlink on PATH: {stripped!r}"
            )

    def test_uses_absolute_venv_interpreter(self):
        src = _SCRIPT.read_text()
        assert "/home/ec2-user/alpha-engine-dashboard/.venv/bin/python" in src

    def test_does_not_source_activate(self):
        for lineno, line in _executable_lines():
            stripped = line.strip()
            assert "source .venv/bin/activate" not in stripped, (
                f"{_SCRIPT.name}:{lineno} sources activate: {stripped!r}"
            )


class TestNoOwnLogShipping:
    def test_does_not_write_under_var_log(self):
        # The krepis.ssm_log_capture wrapper (invoked by the SF) owns
        # tee-to-local-log + S3 ship-on-exit for this script; a second,
        # independent log/trap path here would duplicate the exact
        # `trap 'aws s3 cp ... EXIT'` anti-pattern I7047 exists to remove.
        for lineno, line in _executable_lines():
            assert "/var/log/" not in line, (
                f"{_SCRIPT.name}:{lineno} writes under /var/log/: {line.strip()!r}"
            )

    def test_no_inline_trap_aws_s3_cp_anti_pattern(self):
        for lineno, line in _executable_lines():
            assert "trap 'aws s3 cp" not in line and 'trap "aws s3 cp' not in line, (
                f"{_SCRIPT.name}:{lineno} carries the trap anti-pattern: {line.strip()!r}"
            )


class TestCommandCoverage:
    def test_runs_all_three_checks(self):
        src = _SCRIPT.read_text()
        assert "nousergon_lib.transparency --cadence weekly --alert" in src
        assert "validators.constituents_drift_check" in src
        assert "validators.phase_marker_sweep --run-date" in src

    def test_requires_run_date_argument(self):
        src = _SCRIPT.read_text()
        assert "--run-date" in src
        assert "--run-date is required" in src

    def test_drift_check_does_not_swallow_its_exit_code(self):
        # config#2276 (carried from nousergon-data
        # test_sf_health_check_honesty_wiring.py, which pinned this
        # invariant when the command was inline in the SF definition):
        # constituents_drift_check exits 1 on alert-worthy drift (the
        # 2026-05-23 BNY/P/SN incident surface) — a trailing `|| true`
        # would swallow exactly that signal, and under `set -eo pipefail`
        # its own failure must propagate so the SF poll Choice degrades
        # the completion email.
        for lineno, line in _executable_lines():
            if "constituents_drift_check" in line:
                assert "|| true" not in line, (
                    f"{_SCRIPT.name}:{lineno} swallows the drift check's "
                    f"exit code: {line.strip()!r}"
                )

    def test_phase_marker_sweep_passes_alert_flag(self):
        src = _SCRIPT.read_text()
        sweep_line = next(
            line for line in src.splitlines()
            if "validators.phase_marker_sweep" in line
            and not line.strip().startswith("#")
        )
        assert "--alert" in sweep_line

    def test_phase_marker_sweep_runs_after_constituents_drift(self):
        lines = list(_executable_lines())
        drift_idx = next(
            i for i, (_, line) in enumerate(lines)
            if "validators.constituents_drift_check" in line
        )
        sweep_idx = next(
            i for i, (_, line) in enumerate(lines)
            if "validators.phase_marker_sweep" in line
        )
        assert drift_idx < sweep_idx

    def test_run_date_exported_before_phase_marker_sweep(self):
        lines = list(_executable_lines())
        export_idx = next(
            i for i, (_, line) in enumerate(lines)
            if line.strip() == "export RUN_DATE"
        )
        sweep_idx = next(
            i for i, (_, line) in enumerate(lines)
            if "validators.phase_marker_sweep" in line
        )
        assert export_idx < sweep_idx

    def test_phase_marker_sweep_reads_exported_run_date(self):
        src = _SCRIPT.read_text()
        sweep_line = next(
            line for line in src.splitlines()
            if "validators.phase_marker_sweep" in line
            and not line.strip().startswith("#")
        )
        assert '--run-date "$RUN_DATE"' in sweep_line
