"""Per-stage output assertion wiring (alpha-engine-config-I7214, the ruled
rescope of I7167's end-of-run `StageCoverageAssert` SF-state design).

Brian ruled the shared SF state NON-SOTA: the assertion belongs in each
stage's OWN script, calling the one shared primitive, `krepis.
stage_coverage`, directly — never a per-repo reimplementation of its logic
(`policy-shared-code`'s fork test). `krepis`, not `nousergon_lib`, because
krepis is the fleet's sanctioned bash/runpy entrypoint namespace (`-m
krepis.ssm_dispatcher`, `-m krepis.ec2_spot`, ...) — a `-m
nousergon_lib.<module>` runpy invocation is a guard-less re-export shim on
lib >=0.81.0 that exits 0 WITHOUT executing (config#1646/#1649; see this
repo's own `tests/test_no_runpy_alias_invocation.py`).

SaturdayHealthCheck and WeeklySubstrateHealthCheck are the two weekly-SF
Task states whose commands run scripts out of THIS repo's checkout
(`health_checker.py` and `infrastructure/substrate_health_check.sh`); both
are infrastructure/gate stages that declare no durable artifact
(`COVERED_NO_OUTPUT`).

OBSERVE MODE ONLY: neither call site may set `--enforce` /
`STAGE_COVERAGE_ENFORCE`, and neither call site may be able to make its
script exit non-zero — under `set -eo pipefail`, plus config-I6891 routing
a degraded summary through `CheckDegradedOutcome` -> `DegradedRun` (a Fail
state), a stray non-zero exit here would fail the whole ~4h weekly run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SUBSTRATE_SCRIPT = _REPO_ROOT / "infrastructure" / "substrate_health_check.sh"
_HEALTH_CHECKER_SRC = _REPO_ROOT / "health_checker.py"
_REQUIREMENTS = _REPO_ROOT / "requirements.txt"


def _executable_lines(path: Path):
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if line.strip().startswith("#") or not line.strip():
            continue
        yield lineno, line


# ═══════════════════════════════════════════════════════════════════════════
# substrate_health_check.sh (WeeklySubstrateHealthCheck)
# ═══════════════════════════════════════════════════════════════════════════


class TestSubstrateHealthCheckStageCoverage:
    def test_calls_stage_coverage_assert_with_correct_stage(self):
        src = _SUBSTRATE_SCRIPT.read_text()
        assert "krepis.stage_coverage assert" in src
        assert "--stage WeeklySubstrateHealthCheck" in src

    def test_run_date_is_passed_explicitly(self):
        # alpha-engine-config-I8155: relying on the CLI's implicit $RUN_DATE
        # argparse default is exactly the fragile mechanism this arc
        # removes — assert the assert-line names --run-date explicitly.
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))
        assert_line = next(
            line for _, line in lines if "krepis.stage_coverage assert" in line
        )
        assert "--run-date" in assert_line
        assert '"$RUN_DATE"' in assert_line

    def test_uses_krepis_not_nousergon_lib_namespace(self):
        # config#1646/#1649: `-m nousergon_lib.<module>` is a guard-less
        # re-export shim under runpy on lib >=0.81.0 — silent no-op, not an
        # error. The new call site must live under the sanctioned krepis
        # namespace, not nousergon_lib.
        for _, line in _executable_lines(_SUBSTRATE_SCRIPT):
            if "stage_coverage assert" in line:
                assert "krepis.stage_coverage" in line
                assert "nousergon_lib.stage_coverage" not in line

    def test_uses_shared_primitive_not_a_reimplementation(self):
        # The fork test (policy-shared-code): the I7214 assert line itself
        # may only call the shared lib CLI, never re-derive a stage list or
        # re-read ARTIFACT_REGISTRY.yaml (the I7167 sweep block above it
        # legitimately mentions the registry in its own comments — this
        # guard is scoped to the new executable line only).
        for _, line in _executable_lines(_SUBSTRATE_SCRIPT):
            if "krepis.stage_coverage" in line:
                assert "ARTIFACT_REGISTRY" not in line

    def test_assert_call_cannot_fail_the_script(self):
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))
        assert_line = next(
            line for _, line in lines if "krepis.stage_coverage assert" in line
        )
        # Must be guarded by `|| echo ...` (not a bare call, not `|| true`,
        # and not chained with `&&` which would still propagate a failure).
        assert "|| echo" in assert_line, (
            f"stage-coverage assert call has no failure fallback: {assert_line!r}"
        )
        assert "|| true" not in assert_line, (
            "a bare `|| true` makes an absent module indistinguishable from "
            "a covered stage (config-I7214)"
        )
        assert " && " not in assert_line

    def test_assert_line_is_the_last_work_the_script_does(self):
        # Was `..._is_the_last_executable_line`, on the reasoning that as the
        # script's LAST command its exit status becomes the script's own.
        # config-I7415 made that reasoning obsolete in the safe direction: the
        # script now ends with an aggregate verdict block whose exit status is
        # the three GATING checks' and nothing else, so this observe-mode
        # assertion can no longer reach the exit status even by accident.
        #
        # The invariant that survives is the placement one — the assertion
        # measures what THIS stage wrote, so no further work may run after it.
        # Only the verdict block may.
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))
        assert_idx = next(
            i
            for i, (_, line) in enumerate(lines)
            if "krepis.stage_coverage assert" in line
        )
        after = [line for _, line in lines[assert_idx + 1:]]
        assert after, "the terminal verdict block is missing (config-I7415)"
        for line in after:
            assert any(
                tok in line
                for tok in ("_FAILED_CHECKS", "exit 1", "echo", "if", "fi")
            ), (
                f"line after the stage-coverage assertion does real work: "
                f"{line.strip()!r} — the assertion must measure the stage's "
                f"final state"
            )

    def test_does_not_set_enforce(self):
        # Scoped to the new I7214 assert line — the I7167 sweep block above
        # it legitimately discusses `--enforce` in prose comments about its
        # OWN promotion flip, which is a different mechanism.
        for _, line in _executable_lines(_SUBSTRATE_SCRIPT):
            if "krepis.stage_coverage" in line:
                assert "--enforce" not in line
                assert "STAGE_COVERAGE_ENFORCE" not in line

    def test_window_start_captured_before_the_assert_call(self):
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))
        window_idx = next(
            i for i, (_, line) in enumerate(lines) if "_STAGE_WINDOW_START=" in line
        )
        assert_idx = next(
            i
            for i, (_, line) in enumerate(lines)
            if "krepis.stage_coverage assert" in line
        )
        assert window_idx < assert_idx

    def test_window_start_uses_utc_date(self):
        src = _SUBSTRATE_SCRIPT.read_text()
        assert "date -u +%Y-%m-%dT%H:%M:%SZ" in src

    def test_assert_runs_after_the_existing_three_checks_and_sweep(self):
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))

        def idx(marker):
            return next(i for i, (_, line) in enumerate(lines) if marker in line)

        assert idx("nousergon_lib.transparency") < idx("stage_coverage assert")
        assert idx("validators.constituents_drift_check") < idx("stage_coverage assert")
        assert idx("validators.phase_marker_sweep") < idx("stage_coverage assert")
        assert idx("validators.stage_output_sweep") < idx("stage_coverage assert")


# ═══════════════════════════════════════════════════════════════════════════
# health_checker.py (SaturdayHealthCheck)
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthCheckerStageCoverageSource:
    def test_no_enforce_flag_anywhere_in_source(self):
        src = _HEALTH_CHECKER_SRC.read_text()
        assert "--enforce" not in src
        assert "STAGE_COVERAGE_ENFORCE" not in src

    def test_does_not_reimplement_stage_list_or_read_registry(self):
        # Scoped to the new _assert_stage_coverage function body — an
        # unrelated pre-existing comment elsewhere in this file legitimately
        # references ARTIFACT_REGISTRY.yaml (the universe_membership check).
        src = _HEALTH_CHECKER_SRC.read_text()
        start = src.index("def _assert_stage_coverage(")
        end = src.index("\ndef main():")
        func_body = src[start:end]
        assert "ARTIFACT_REGISTRY" not in func_body

    def test_main_calls_assert_stage_coverage_before_sys_exit(self):
        # Static ordering guard: the assertion call must precede the final
        # sys.exit so it always runs on the success path.
        src = _HEALTH_CHECKER_SRC.read_text()
        assert_idx = src.rindex("_assert_stage_coverage(")
        exit_idx = src.rindex("sys.exit(1 if failures else 0)")
        assert assert_idx < exit_idx


class TestHealthCheckerStageCoverageBehavior:
    def test_calls_shared_cli_with_correct_stage_and_window(self):
        import health_checker

        with patch("health_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            health_checker._assert_stage_coverage(
                "SaturdayHealthCheck", "2026-08-15T09:00:00Z", "2026-08-15",
            )

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[1:5] == ["-m", "krepis.stage_coverage", "assert", "--stage"]
        assert "SaturdayHealthCheck" in cmd
        assert "--window-start" in cmd
        assert "2026-08-15T09:00:00Z" in cmd
        assert "--run-date" in cmd
        assert "2026-08-15" in cmd
        assert kwargs.get("check") is False

    def test_nonzero_return_code_does_not_raise(self):
        import health_checker

        with patch("health_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3)
            # Must not raise — observe mode, the caller's exit code is
            # untouched by this call.
            health_checker._assert_stage_coverage(
                "SaturdayHealthCheck", "2026-08-15T09:00:00Z", "2026-08-15",
            )

    def test_module_not_found_does_not_raise(self):
        # Simulates the module not existing yet (the shared krepis
        # primitive lands in a separate, not-yet-merged PR) — subprocess.run
        # itself raising is the worst case this call site must survive.
        import health_checker

        with patch("health_checker.subprocess.run", side_effect=FileNotFoundError("no such file")):
            health_checker._assert_stage_coverage(
                "SaturdayHealthCheck", "2026-08-15T09:00:00Z", "2026-08-15",
            )

    def test_absent_run_date_skips_the_assertion_without_fabricating(self, capsys):
        # alpha-engine-config-I8155: never fabricate a date to satisfy the
        # (now-required) run_date argument — an absent EXECUTION_RUN_DATE
        # must skip the subprocess call entirely, loudly, never substitute
        # datetime.now().
        import health_checker

        with patch("health_checker.subprocess.run") as mock_run:
            health_checker._assert_stage_coverage(
                "SaturdayHealthCheck", "2026-08-15T09:00:00Z", "",
            )
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert "SaturdayHealthCheck" in captured.err

    def test_main_threads_execution_run_date_env_into_the_assertion(self, monkeypatch):
        # alpha-engine-config-I8155: the SaturdayHealthCheck SF Task state
        # does not (yet) pass a run_date in its Payload — the execution
        # identity is read from $EXECUTION_RUN_DATE (exported by the SF
        # definition; nousergon-data's peer PR for this arc). --run-date's
        # argparse default reads it, and main() threads that through to
        # _assert_stage_coverage.
        monkeypatch.setenv("EXECUTION_RUN_DATE", "2026-08-22")
        import importlib

        import health_checker

        importlib.reload(health_checker)
        try:
            with patch("health_checker.check_all", return_value=[]), \
                 patch("health_checker._emit_cloudwatch_metrics"), \
                 patch("health_checker._assert_stage_coverage") as mock_assert, \
                 patch("health_checker.sys.exit"), \
                 patch("sys.argv", ["health_checker.py"]):
                health_checker.main()
            mock_assert.assert_called_once()
            assert mock_assert.call_args[0][2] == "2026-08-22"
        finally:
            monkeypatch.delenv("EXECUTION_RUN_DATE", raising=False)
            importlib.reload(health_checker)

    def test_main_invokes_assert_stage_coverage_with_saturday_health_check(self, capsys):
        import health_checker

        with patch("health_checker.check_all", return_value=[]), \
             patch("health_checker._emit_cloudwatch_metrics"), \
             patch("health_checker._assert_stage_coverage") as mock_assert, \
             patch("health_checker.sys.exit") as mock_exit, \
             patch("sys.argv", ["health_checker.py"]):
            health_checker.main()

        mock_assert.assert_called_once()
        stage_arg = mock_assert.call_args[0][0]
        assert stage_arg == "SaturdayHealthCheck"
        mock_exit.assert_called_once_with(0)


# ═══════════════════════════════════════════════════════════════════════════
# Cross-cutting: the krepis pin must not REGRESS below stage_coverage
# ═══════════════════════════════════════════════════════════════════════════


class TestKrepisPinCarriesStageCoverage:
    """Succeeds `TestKrepisPinNotBumped`, whose precondition is now met.

    That guard asserted the pin still read `==0.54.0`. Its stated purpose
    (config-I7214) was to stop the bump riding along inside the
    stage-coverage PR — *"lands ONLY after the krepis PR carrying
    `stage_coverage` merges and releases to PyPI"* — by forcing it through
    its own reviewed diff.

    Both halves of that condition are now satisfied, measured 2026-08-13:
    `krepis` 0.59.3 is on PyPI and `src/krepis/stage_coverage.py` is present
    on its `main`. This PR IS the separate reviewed diff the guard demanded.
    Leaving the old assertion in place would not preserve a safeguard — it
    would make the bump it was designed to sequence permanently impossible,
    which is the failure mode of every test that pins a version literal
    instead of the property it cares about.

    So the guard is inverted rather than deleted: it protected against a
    PREMATURE bump, and now protects against a REGRESSION below the version
    that carries the module the dashboard depends on.
    """

    # The first krepis release carrying src/krepis/stage_coverage.py.
    MIN_STAGE_COVERAGE_VERSION = (0, 59, 3)

    def test_pin_is_at_or_above_the_stage_coverage_release(self):
        import re

        src = _REQUIREMENTS.read_text()
        # Extras are matched as a SET, not as a literal substring: uv emits
        # `krepis[flow-doctor, openai]==` (a space after the comma) under
        # --no-strip-extras, and PEP 508 permits whitespace there. Pinning the
        # exact spelling made this test fail on a lock uv itself produced
        # (alpha-engine-config-I8309), which is the version-literal failure
        # mode this class's own docstring warns about.
        match = re.search(
            r"^krepis\[([^\]]*)\]==(\d+)\.(\d+)\.(\d+)", src, re.MULTILINE
        )
        assert match, (
            "no pinned krepis[...]==X.Y.Z line found in "
            f"{_REQUIREMENTS.name} — the dashboard box's interpreter is the "
            "one every weekly-SF stage runs through; it must be pinned."
        )
        extras = {e.strip() for e in match.group(1).split(",")}
        assert {"flow-doctor", "openai"} <= extras, (
            f"the krepis lock line carries extras {sorted(extras)} — both "
            "[flow-doctor] (the FlowDoctorHandler) and [openai] "
            "(live/morning_brief.py's OpenRouter transport) must survive the "
            "compile. A `uv pip compile` without --no-strip-extras drops them."
        )
        pinned = tuple(int(g) for g in match.groups()[1:])
        assert pinned >= self.MIN_STAGE_COVERAGE_VERSION, (
            f"krepis pinned at {'.'.join(map(str, pinned))}, below "
            f"{'.'.join(map(str, self.MIN_STAGE_COVERAGE_VERSION))} which is the "
            "first release carrying src/krepis/stage_coverage.py. The dashboard "
            "imports it; an older pin fails at runtime on the box, not here."
        )
