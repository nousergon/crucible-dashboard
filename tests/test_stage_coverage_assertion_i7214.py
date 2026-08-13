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
        assert "nousergon_lib.stage_coverage assert" in src
        assert "--stage WeeklySubstrateHealthCheck" in src

    def test_uses_the_real_nousergon_lib_module_not_a_krepis_shim(self):
        # config#1649 forbids `-m nousergon_lib.<module>` in box scripts
        # because for KREPIS-EXTRACTED modules it resolves to a guard-less
        # re-export shim that exits 0 doing nothing (config#1646 — the class
        # that ran a whole weekly SF with zero work).
        #
        # stage_coverage is NOT such a module. It was added to nousergon-lib
        # as a real module by nousergon-lib-PR314 (merged 2026-08-13T19:03:24Z)
        # and `krepis` has no stage_coverage and does not re-export
        # nousergon_lib — so there is no shim to fall through to, and pointing
        # at krepis would invoke a module that does not exist. It is therefore
        # registered in _REAL_NL_MODULE_EXEMPTIONS in
        # tests/test_no_runpy_alias_invocation.py, which is the escape that
        # guard defines for exactly this case.
        found = False
        for _, line in _executable_lines(_SUBSTRATE_SCRIPT):
            if "stage_coverage assert" in line:
                found = True
                assert "nousergon_lib.stage_coverage" in line, line
                assert "krepis.stage_coverage" not in line, line
        assert found, "no stage_coverage assert line found in the substrate script"

    def test_uses_shared_primitive_not_a_reimplementation(self):
        # The fork test (policy-shared-code): the I7214 assert line itself
        # may only call the shared lib CLI, never re-derive a stage list or
        # re-read ARTIFACT_REGISTRY.yaml (the I7167 sweep block above it
        # legitimately mentions the registry in its own comments — this
        # guard is scoped to the new executable line only).
        for _, line in _executable_lines(_SUBSTRATE_SCRIPT):
            if "nousergon_lib.stage_coverage" in line:
                assert "ARTIFACT_REGISTRY" not in line

    def test_assert_call_cannot_fail_the_script(self):
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))
        assert_line = next(
            line for _, line in lines if "nousergon_lib.stage_coverage assert" in line
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

    def test_assert_line_is_the_last_executable_line(self):
        # The riskiest placement: as the script's LAST command, its exit
        # status becomes the script's own exit status. Confirm the `||
        # echo` fallback (return 0) is genuinely the tail of the script.
        lines = list(_executable_lines(_SUBSTRATE_SCRIPT))
        assert "nousergon_lib.stage_coverage assert" in lines[-1][1]

    def test_does_not_set_enforce(self):
        # Scoped to the new I7214 assert line — the I7167 sweep block above
        # it legitimately discusses `--enforce` in prose comments about its
        # OWN promotion flip, which is a different mechanism.
        for _, line in _executable_lines(_SUBSTRATE_SCRIPT):
            if "nousergon_lib.stage_coverage" in line:
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
            if "nousergon_lib.stage_coverage assert" in line
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
            health_checker._assert_stage_coverage("SaturdayHealthCheck", "2026-08-15T09:00:00Z")

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[1:5] == ["-m", "nousergon_lib.stage_coverage", "assert", "--stage"]
        assert "SaturdayHealthCheck" in cmd
        assert "--window-start" in cmd
        assert "2026-08-15T09:00:00Z" in cmd
        assert kwargs.get("check") is False

    def test_nonzero_return_code_does_not_raise(self):
        import health_checker

        with patch("health_checker.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3)
            # Must not raise — observe mode, the caller's exit code is
            # untouched by this call.
            health_checker._assert_stage_coverage("SaturdayHealthCheck", "2026-08-15T09:00:00Z")

    def test_module_not_found_does_not_raise(self):
        # Simulates the module not existing yet (the shared krepis
        # primitive lands in a separate, not-yet-merged PR) — subprocess.run
        # itself raising is the worst case this call site must survive.
        import health_checker

        with patch("health_checker.subprocess.run", side_effect=FileNotFoundError("no such file")):
            health_checker._assert_stage_coverage("SaturdayHealthCheck", "2026-08-15T09:00:00Z")

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
# Cross-cutting: the krepis pin is NOT bumped in this PR
# ═══════════════════════════════════════════════════════════════════════════


class TestKrepisPinNotBumped:
    def test_pin_still_names_the_pre_stage_coverage_version(self):
        # config-I7214: the krepis bump is a separate, mechanical wave that
        # lands ONLY after the krepis PR carrying `stage_coverage` merges
        # and releases to PyPI — pinning a guessed version now would be a
        # defect (the version does not exist yet). This guard pins the
        # CURRENT version so a same-PR bump trips it, forcing the bump to
        # go through its own reviewed diff.
        src = _REQUIREMENTS.read_text()
        assert "krepis[flow-doctor,openai]==0.54.0" in src
