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

import re
import pytest
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
        assert "/home/ec2-user/alpha-engine-data/.venv/bin/python" in src

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

    def test_phase_marker_sweep_does_not_pass_a_flag_the_sweep_does_not_declare(self):
        """`--alert` is not an option of validators.phase_marker_sweep.

        config-I7415. The sweep alerts by DEFAULT (`alert=not args.no_alert`)
        and declares `--no-alert` / `--alert-severity`. argparse accepted
        `--alert` as an unambiguous prefix of `--alert-severity`, which then
        aborted with `expected one argument` — so the sweep exited 2 without
        sweeping, on every weekly run since the flag was added, and the caller
        recorded that usage error as a failing gating check.

        This test previously asserted `--alert` was PRESENT: a guard keyed on
        the defect, which passed on its own bug. Turning alerting on is the
        default; the only correct assertion is that alerting is not turned OFF.
        """
        src = _SCRIPT.read_text()
        sweep_line = next(
            line for line in src.splitlines()
            if "validators.phase_marker_sweep" in line
            and not line.strip().startswith("#")
        )
        assert "--alert" not in sweep_line, (
            f"{_SCRIPT.name} passes an undeclared flag to phase_marker_sweep, "
            f"which argparse rebinds to --alert-severity: {sweep_line.strip()!r}"
        )
        assert "--no-alert" not in sweep_line, (
            "the weekly sweep must alert; --no-alert is diagnostic-mode only"
        )

    def test_run_check_captures_the_real_exit_code(self):
        """`rc` must be captured on the failing command, not after an `if`.

        config-I7415. `local rc=$?` placed after an `if "$@"; then ... fi`
        block reads the exit status of the *`if` construct*, which is 0 by
        definition — so every failure the helper ever reported carried
        `rc=0`. Measured on ne-weekly-freshness-pipeline execution
        `watch-rerun-2026-08-15-2` (2026-08-15): three distinct failures,
        three identical `(rc=0)`.
        """
        src = _SCRIPT.read_text()
        body = src.split("run_check() {", 1)[1].split("\n}", 1)[0]
        assert 'rc=$?' in body, "run_check must record the check's exit code"
        # The capture has to be attached to the invocation itself.
        assert re.search(r'"\$@"\s*\|\|\s*rc=\$\?', body), (
            "run_check must capture rc on the same command as the failure; "
            f"body was: {body!r}"
        )
        assert 'if "$@"; then' not in body, (
            "the `if`-then-capture shape always yields rc=0"
        )

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


class TestOneRunMeasuresTheWholeSurface:
    """alpha-engine-config-I7415.

    The three gating checks used to run as bare commands under `set -e`, so
    the FIRST non-zero exit aborted the script and the rest never ran. On a
    tail health check of an already-finished ~4h pipeline there is nothing
    downstream to protect by stopping early — the only thing the abort bought
    was that each Saturday revealed exactly one problem, at a cost of one
    four-hour run per finding. Measured 2026-08-15.
    """

    def test_every_gating_check_goes_through_run_check(self):
        """A bare invocation is the regression: it restores first-failure-wins
        for whichever check is added next."""
        for lineno, line in _executable_lines():
            for mod in (
                "nousergon_lib.transparency",
                "validators.constituents_drift_check",
                "validators.phase_marker_sweep",
            ):
                if mod in line:
                    # the invocation must be an argument to run_check, which
                    # means the preceding non-blank executable line opens one.
                    assert "run_check" in _preceding_run_check_block(lineno), (
                        f"{_SCRIPT.name}:{lineno} invokes {mod} outside "
                        f"run_check — its failure would abort the remaining "
                        f"gating checks under set -e"
                    )

    def test_a_failed_check_still_exits_non_zero(self):
        src = _SCRIPT.read_text()
        assert "_FAILED_CHECKS" in src
        assert "exit 1" in src

    def test_the_failure_summary_is_the_last_thing_written(self):
        """krepis.ssm_log_capture quotes the command's LAST output line when it
        summarises a non-zero exit, so the summary has to BE the last line —
        the 2026-08-15 run's DEGRADED reason named a non-fatal row instead
        (config-I7393)."""
        lines = [line for _, line in _executable_lines()]
        exit_idx = next(
            i for i, line in enumerate(lines) if line.strip() == "exit 1"
        )
        summary_idx = next(
            i for i, line in enumerate(lines)
            if "EXIT 1 —" in line and "_FAILED_CHECKS[*]" in line
        )
        assert summary_idx == exit_idx - 1, (
            "the failure summary must be the last line written before exiting"
        )

    def test_observe_mode_checks_never_reach_the_failure_list(self):
        """The stage-output sweep and the stage-coverage assertion are
        observe-mode by ruling (detect before enforcing when the floor is
        unmeasured, Brian 2026-08-11) — they must not be able to fail the
        run."""
        for lineno, line in _executable_lines():
            if "stage_output_sweep" in line or "krepis.stage_coverage" in line:
                assert "run_check" not in line, (
                    f"{_SCRIPT.name}:{lineno} routes an observe-mode check "
                    f"through run_check, which would let it fail a four-hour "
                    f"production run"
                )


def _preceding_run_check_block(lineno: int) -> str:
    """The two executable lines ending at ``lineno`` — enough to see the
    ``run_check`` that a wrapped invocation is an argument to."""
    lines = list(_executable_lines())
    idx = next(i for i, (ln, _) in enumerate(lines) if ln == lineno)
    return "\n".join(line for _, line in lines[max(0, idx - 1): idx + 1])

class TestPerRepoInterpreter:
    """config-I7427: each repo's code runs under that repo's own venv.

    Three of the four checks are alpha-engine-data's code. Running them under
    the DASHBOARD venv meant the constituents drift check never once reached
    its comparison — `No module named 'arcticdb'`, and an `openpyxl` import
    failure that reported `0 tickers` as a RESULT (measured 2026-08-15,
    weekly-SF execution watch-rerun-2026-08-15-2). The two closures cannot be
    merged: the dashboard venv is pinned numpy<2 and alpha-engine-data
    declares numpy>=2.4.6.
    """

    def _line_for(self, module: str) -> str:
        return next(
            line for _, line in _executable_lines() if module in line
        )

    @pytest.mark.parametrize("module", [
        "validators.constituents_drift_check",
        "validators.phase_marker_sweep",
        "validators.stage_output_sweep",
    ])
    def test_alpha_engine_data_modules_use_the_data_interpreter(self, module):
        line = self._line_for(module)
        assert "$DATA_PYTHON_BIN" in line, (
            f"{module} runs under the dashboard venv, whose closure has "
            f"neither arcticdb nor openpyxl: {line.strip()!r}"
        )

    @pytest.mark.parametrize("module", [
        "nousergon_lib.transparency",
        "krepis.stage_coverage",
    ])
    def test_dashboard_modules_use_the_dashboard_interpreter(self, module):
        line = self._line_for(module)
        assert "$DATA_PYTHON_BIN" not in line, (
            f"{module} is resolved from the dashboard venv, not the data one: "
            f"{line.strip()!r}"
        )

    def test_a_missing_data_venv_aborts_rather_than_falling_back(self):
        """No fallback to PYTHON_BIN.

        A fallback restores the silent wrong-interpreter state this fixes, on
        exactly the day the bootstrap stopped building the venv.
        """
        src = _SCRIPT.read_text()
        assert 'if [[ ! -x "$DATA_PYTHON_BIN" ]]; then' in src
        assert "DATA_PYTHON_BIN=${PYTHON_BIN" not in src, (
            "the data interpreter must not default to the dashboard one"
        )
        assert 'DATA_PYTHON_BIN:-' not in src, (
            "the data interpreter must not carry a fallback default"
        )

