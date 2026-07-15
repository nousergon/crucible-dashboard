"""Verify flow-doctor wiring in dashboard entrypoints.

The dashboard repo had ZERO flow-doctor wiring before this PR — the
yaml existed at the repo root but no source file imported
nousergon_lib or flow_doctor. This PR closes the gap as the
fifth and final step in the cross-repo wire-quality arc.

Asserts the canonical alpha-engine-lib pattern is in place for both
dashboard entrypoints:

- ``app.py``              — Streamlit Home page (multi-page app entry)
- ``health_checker.py``   — staleness CLI (called by systemd / operator)

The dashboard runs as a long-lived Streamlit process on EC2
(ae-dashboard t3.micro), NOT a Lambda — no cold-start init-timeout
concerns and no LAMBDA_TASK_ROOT path resolution. Pages under
``pages/*`` inherit the root logger configuration via Python's
logging-hierarchy propagation; their ``logger = logging.getLogger(__name__)``
calls resolve to child loggers that propagate to the root handler
attached by ``app.py`` at startup.

Runs without firing any LLM diagnosis: setup_logging is exercised
with FLOW_DOCTOR_ENABLED=1 + stub env vars + a redirected yaml store
path, but no ERROR records are emitted.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def stub_flow_doctor_env(monkeypatch):
    """Populate the env vars that flow-doctor.yaml's ${VAR} refs resolve."""
    monkeypatch.setenv("FLOW_DOCTOR_ENABLED", "1")
    monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "test@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "stub-password")
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "stub-token")


@pytest.fixture
def reset_root_logger():
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    root.handlers = saved


@pytest.fixture
def temp_flow_doctor_yaml(tmp_path):
    """Hand-written yaml with the minimum schema setup_logging needs:
    email notifier (so flow_doctor.init() succeeds) + sqlite store at
    tmp_path. Avoids any yaml.safe_load() that could be patched by
    tests/test_s3_loader*.py.

    flow_doctor v0.3.0 preflights GitHub tokens via api.github.com so
    the github channel is intentionally omitted (stub token would
    401)."""
    yaml_path = tmp_path / "flow-doctor.yaml"
    db_path = tmp_path / "flow_doctor_test.db"
    yaml_path.write_text(
        f"flow_name: dashboard-test\n"
        f"repo: nousergon/crucible-dashboard\n"
        f"owner: \"@brianmcmahon\"\n"
        f"notify:\n"
        f"  - type: email\n"
        f"    sender: ${{EMAIL_SENDER}}\n"
        f"    recipients: ${{EMAIL_RECIPIENTS}}\n"
        f"    smtp_host: smtp.gmail.com\n"
        f"    smtp_port: 587\n"
        f"    smtp_password: ${{GMAIL_APP_PASSWORD}}\n"
        f"store:\n"
        f"  type: sqlite\n"
        f"  path: {db_path}\n"
        f"dedup_cooldown_minutes: 60\n"
        f"rate_limits:\n"
        f"  max_alerts_per_day: 10\n"
    )
    return str(yaml_path)


def _flow_doctor_available() -> bool:
    try:
        import flow_doctor  # noqa: F401
        return True
    except ImportError:
        return False


flow_doctor_required = pytest.mark.skipif(
    not _flow_doctor_available(),
    reason="flow-doctor not installed (pip install alpha-engine-lib[flow_doctor])",
)


class TestFlowDoctorYamlPresence:
    """flow-doctor.yaml must exist at the repo root and resolve from
    each entrypoint's path computation."""

    def test_yaml_at_repo_root_exists(self):
        assert (REPO_ROOT / "flow-doctor.yaml").is_file()

    def test_yaml_path_resolved_by_app_exists(self):
        # app.py: os.path.dirname(os.path.abspath(__file__))
        app_path = REPO_ROOT / "app.py"
        resolved = Path(os.path.dirname(os.path.abspath(app_path))) / "flow-doctor.yaml"
        assert resolved.is_file(), f"app.py resolves to {resolved}"

    def test_yaml_path_resolved_by_health_checker_exists(self):
        hc_path = REPO_ROOT / "health_checker.py"
        resolved = Path(os.path.dirname(os.path.abspath(hc_path))) / "flow-doctor.yaml"
        assert resolved.is_file(), f"health_checker.py resolves to {resolved}"


class TestFlowDoctorYamlSchema:
    """flow-doctor.yaml must declare keys consistent with the lib contract.

    Uses text-based assertions instead of yaml.safe_load() because
    tests/test_s3_loader*.py patches yaml.safe_load via context
    managers; cross-test ordering can leak a MagicMock return value
    into this test if the patch is still resolved on lookup. Reading
    the file as plain text is patch-immune and the yaml shape we care
    about is small + stable.
    """

    @staticmethod
    def _read_yaml_text() -> str:
        return (REPO_ROOT / "flow-doctor.yaml").read_text()

    def test_yaml_has_required_top_level_keys(self):
        text = self._read_yaml_text()
        for key in ("flow_name", "repo", "notify", "store", "rate_limits"):
            # Top-level keys appear at column 0 followed by `:`.
            assert (
                f"\n{key}:" in "\n" + text
            ), f"missing top-level key: {key}"
        assert "repo: nousergon/crucible-dashboard" in text

    def test_yaml_has_email_notify_channel(self):
        text = self._read_yaml_text()
        # `- type: email` appears under `notify:` as a list item.
        assert "- type: email" in text, (
            "flow-doctor.yaml must declare an email notify channel"
        )

    def test_yaml_github_routes_to_config_backlog(self):
        text = self._read_yaml_text()
        assert "repo: nousergon/alpha-engine-config" in text
        assert "notify_on_category: [CODE, CONFIG]" in text
        assert "area:dashboard" in text
        assert "- type: telegram" in text
        assert "notify_on_category: [TRANSIENT, EXTERNAL, INFRA]" in text


@flow_doctor_required
class TestSetupLoggingAttach:
    """setup_logging() should attach FlowDoctorHandler when ENABLED=1.

    Does NOT fire any ERROR records, so flow-doctor's diagnose() /
    Anthropic calls are never invoked.
    """

    def test_disabled_attaches_no_flow_doctor_handler(self, monkeypatch, reset_root_logger):
        monkeypatch.setenv("FLOW_DOCTOR_ENABLED", "0")
        from nousergon_lib.logging import setup_logging
        setup_logging(
            "dashboard-test-disabled",
            flow_doctor_yaml=str(REPO_ROOT / "flow-doctor.yaml"),
            exclude_patterns=[],
        )
        import flow_doctor
        attached = [h for h in logging.getLogger().handlers
                    if isinstance(h, flow_doctor.FlowDoctorHandler)]
        assert attached == []

    def test_enabled_attaches_flow_doctor_handler(
        self, stub_flow_doctor_env, reset_root_logger, temp_flow_doctor_yaml
    ):
        from nousergon_lib.logging import setup_logging, get_flow_doctor
        setup_logging(
            "dashboard-test-enabled",
            flow_doctor_yaml=temp_flow_doctor_yaml,
            exclude_patterns=[],
        )
        import flow_doctor
        attached = [h for h in logging.getLogger().handlers
                    if isinstance(h, flow_doctor.FlowDoctorHandler)]
        assert len(attached) == 1
        assert get_flow_doctor() is not None

    def test_exclude_patterns_plumbed_to_handler(
        self, stub_flow_doctor_env, reset_root_logger, temp_flow_doctor_yaml
    ):
        from nousergon_lib.logging import setup_logging
        patterns = [r"streamlit cache miss", r"S3 ClientError NoSuchKey"]
        setup_logging(
            "dashboard-test-patterns",
            flow_doctor_yaml=temp_flow_doctor_yaml,
            exclude_patterns=patterns,
        )
        import flow_doctor
        attached = [h for h in logging.getLogger().handlers
                    if isinstance(h, flow_doctor.FlowDoctorHandler)]
        assert len(attached) == 1
        compiled = attached[0]._exclude_re
        assert [p.pattern for p in compiled] == patterns


class TestEntrypointModuleTopWiring:
    """Each entrypoint must call setup_logging at MODULE-TOP, not inside a
    function. Source-text checks; no flow_doctor.init() side effects.
    """

    @staticmethod
    def _index_of(needle: str, text: str) -> int:
        idx = text.find(needle)
        assert idx != -1, f"missing required text: {needle!r}"
        return idx

    @staticmethod
    def _strip_comments_and_docstrings(text: str) -> str:
        import re
        stripped = re.sub(r'"""[\s\S]*?"""', "", text)
        stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.MULTILINE)
        return stripped

    def test_app_calls_setup_logging_at_module_top(self):
        text = (REPO_ROOT / "app.py").read_text()
        setup_idx = self._index_of("setup_logging(", text)
        # app.py has no def main(); the top-level Streamlit page code
        # runs as module-level statements. setup_logging must come
        # before any pandas / streamlit import so import-time errors
        # are captured by the root handler.
        st_import_idx = text.find("import streamlit")
        assert st_import_idx != -1, "expected `import streamlit` somewhere in app.py"
        assert setup_idx < st_import_idx, (
            "setup_logging must run before `import streamlit` so streamlit "
            "import-time errors are captured by flow-doctor's root handler"
        )
        assert "exclude_patterns=" in text[setup_idx:setup_idx + 500]

    def test_health_checker_calls_setup_logging_at_module_top(self):
        text = (REPO_ROOT / "health_checker.py").read_text()
        setup_idx = self._index_of("setup_logging(", text)
        main_def_idx = self._index_of("def main(", text)
        assert setup_idx < main_def_idx, (
            "health_checker setup_logging must be at module-top, before def main()"
        )
        assert "exclude_patterns=" in text[setup_idx:main_def_idx]
        body = self._strip_comments_and_docstrings(text[main_def_idx:])
        assert "setup_logging(" not in body, (
            "duplicate setup_logging call inside main() — should only run once"
        )
        # The pre-PR-5 logging.basicConfig() call must be gone (it would
        # clobber the root handler that setup_logging just installed).
        assert "logging.basicConfig(" not in body, (
            "logging.basicConfig() inside main() clobbers setup_logging's "
            "root handler; use logging.getLogger().setLevel() instead"
        )


class TestLibVersionPin:
    """alpha-engine-lib must be pinned to a stable tag, not @main."""

    def test_requirements_pins_lib_to_stable_tag(self):
        text = (REPO_ROOT / "requirements.txt").read_text()
        assert "nousergon-lib" in text, (
            "nousergon-lib must be a declared dependency for setup_logging "
            "to be importable (renamed from alpha-engine-lib at v0.60.0; the "
            "nousergon_lib import alias still works via the shim)"
        )
        assert "@main" not in text, "nousergon-lib must be pinned to a tag, not @main"
        assert "@v0.116.0" in text, (
            "nousergon-lib should pin to v0.116.0 (alpha-engine-config#2557, "
            "nousergon-lib#207: pipeline_status registry's "
            "EvalJudgeSubmitFirstSaturday/EvalJudgeSubmitWeekly/"
            "EvalJudgeProcess/EvalRollingMean ArchivePageRefs follow this "
            "repo's host_eval_backtester?tab=Eval+Quality -> evaluator "
            "collapse — the vestigial single-tab host wrapper was deleted; "
            "8_Eval_Quality.py is now a standalone st.Page at "
            "url_path=\"evaluator\"; keeps "
            "test_registry_page_targets.py::test_every_archive_page_ref_resolves "
            "green. Note: #207 itself landed the registry change AND (per "
            "its own pyproject-version-bump-check guard on src/ changes) a "
            "version bump to 0.116.0 in the same PR, so v0.115.0 predates "
            "the fix and does not carry it — 0.116.0 is the first tag that "
            "actually resolves the registry entries. "
            "Earlier: v0.111.0 (alpha-engine-config-I2430, "
            "4th recurrence of the config#1115/#2372 pipeline-status registry "
            "drift class: nousergon-lib#188 [v0.108.0] registers 7 Saturday-SF "
            "gate/health-degraded states — PublishLibPinGateDegraded / "
            "PublishPipelineContractGateDegraded / NotifyCompleteGatesDegraded / "
            "NotifyCompleteHealthDegraded / NotifyCompleteGatesAndHealthDegraded "
            "/ PublishReportCardDegraded / PublishDirectorDegraded — and "
            "nousergon-lib#194 [v0.111.0] registers ThinkTankCoverage "
            "[alpha-engine-config-I2467's Think Tank coverage-fill Lambda]; "
            "both needed to clear "
            "test_pipeline_status_registry_drift.py::"
            "test_every_substantive_state_has_registry_entry[Saturday-json_path0]). "
            "Earlier: v0.107.0 (nousergon-lib#185: "
            "pipeline_status.registry's AggregateCosts ArchivePageRef follows "
            "this repo's LLM Cost/Usage -> API/Plan tab rename, "
            "host_cost_usage?tab=LLM+Cost -> ?tab=API; keeps "
            "test_registry_page_targets.py::test_every_archive_page_ref_resolves "
            "green, crucible-dashboard#391). "
            "Earlier: v0.96.0 (pipeline_status registry "
            "entries for the config#1687 data-spot SF states — LaunchMorning"
            "EnrichSpot/LaunchMorningArcticAppendSpot/LaunchPostMarketDataSpot/"
            "LaunchPostMarketArcticAppendSpot/PublishDataSpotFailureImmediate — "
            "plus every ArchivePageRef repointed at a live console URL after "
            "the console-IA redesign (config#1990); clears the local "
            "cross-repo drift guard test_pipeline_status_registry_drift). "
            "Earlier: v0.89.0 (2026-07-06 SF states — WeeklyRunDayGate/"
            "WeeklyRunDayGateFailed (config#1824), CodeFreshnessGate + "
            "WaitForCodeFreshness + ForceStopUnresponsiveInstance "
            "(config#1811), LaunchDailyDataSpot (config#1807)). "
            "Earlier: v0.87.0 (nousergon_lib.health registry-aligned "
            "keys + dashboard constants for config#1728 Phase D). "
            "the EOD SF's RefreshExecutorDeploy top-of-pipeline executor-"
            "checkout refresh chokepoint (config#1549) + its "
            "WaitForRefreshExecutorDeploy companion in "
            "nousergon_lib.pipeline_status.registry — clears the local "
            "cross-repo drift guard test_pipeline_status_registry_drift). "
            "Earlier: v0.80.0 (config#1587: registers the "
            "EOD SF's StartTradingInstance re-runnability guard state "
            "(nousergon-data#576) in nousergon_lib.pipeline_status.registry, "
            "nousergon-lib#151). "
            "Earlier: v0.79.0 (EPIC config#1483 consumer "
            "cutover, config#1531: loaders/outcome_store.py reads the "
            "long-format score_performance_outcomes store via "
            "nousergon_lib.quant.horizons.HorizonPolicy instead of hardcoded "
            "wide horizon-suffixed score_performance column literals; the "
            "burn-down guard test imports the shared "
            "nousergon_lib.quant.horizon_guard ratchet primitive lifted to "
            "the lib on the second adoption, nousergon-lib#149 / 0.78.0). "
            "Earlier: v0.73.0 (config#647 step 3 fleet "
            "re-pin off the flow-doctor release candidate: lib <= v0.67.0 "
            "hard-pinned the flow_doctor extra to "
            "`flow-doctor[diagnosis,s3]==0.6.0rc6`; v0.68.0 delegated the extra "
            "to `krepis[flow_doctor]`, and krepis v0.5.0 floors "
            "`flow-doctor>=0.6.0,<0.7`, so this bump moves the dashboard's "
            "flow-doctor off rc6 onto the 0.6.0 final that shipped via "
            "flow-doctor#34 / tag v0.6.0 on PyPI; v0.73.0 is a strict superset "
            "of the pipeline_status/transparency APIs the dashboard consumes). "
            "Earlier: v0.60.2 (alpha_engine_lib alias "
            "resource-loading shim fix — preserves the real module __spec__ so "
            "transparency package-data loads when the box runs "
            "`python -m alpha_engine_lib.transparency`; plus the v0.60.1 "
            "SSM-authoritative flow-doctor secret seed that overwrites a stale "
            "sourced FLOW_DOCTOR_GITHUB_TOKEN so the github_issue notifier stops "
            "403ing; config#1148). Earlier: v0.59.7 "
            "(pipeline-status registry: "
            "the EOD PostMarketArcticAppend state + WaitForPostMarketArcticAppend "
            "companion — required so test_pipeline_status_registry_drift passes "
            "for the EOD SF; config#1120). Earlier: v0.59.6 (config#1083 model-zoo "
            "fan-out states ResolveZooSpecs / TrainSpecDispatch / ModelZooSelect / "
            "PublishModelZooFailureImmediate; config#1115). Earlier: v0.59.2 "
            "(ChronicGapSelfHeal [L4604] + MorningArcticAppend [L4608] weekday "
            "resilience states + their WaitFor* companions — the 2026-06-11 "
            "weekday-SF split arc; v0.59.x also carries the M0 slot contracts). "
            "Earlier: v0.57.1 (ModelZooRotation [L4544/L4571] + LibPinDriftCheck "
            "[L4517] + RunDailyNews + WaitFor* companions). Earlier: v0.52.1 "
            "(pipeline-status registry: "
            "ReportCard SF-state entry — the Director arc added a ReportCard "
            "Task state to the Saturday SF (alpha-engine-data #367), required so "
            "test_pipeline_status_registry_drift passes + page-25 deep-links it "
            "to the Report Card page; also v0.52.0 MetricRecord + quant.stats "
            "intervals the Report Card v2 surface consumes). v0.45.0 (L4472 "
            "pipeline-status registry: PredictorBacktest + PortfolioOptimizerBacktest split "
            "states — required so test_pipeline_status_registry_drift passes "
            "after the backtester phase-split; v0.44 added the L249 per-cycle "
            "artifact-completion rollup). Earlier: v0.43 (transparency non-fatal "
            "`degraded` status — artifact-completion-monitoring Phase 1a/1c; "
            "activates the substrate-health check run from this venv). "
            "Earlier version history retained: "
            "v0.36.1 (Option-D execution-picker + AggregateCosts registry "
            "entry — Page 25 needs v0.36.0+ to filter SF executions by "
            "pipeline_role so smoke / recovery / operator-replay runs "
            "don't displace the canonical cadence run as 'most recent'); "
            "v0.37 (anthropic_payload chokepoint), v0.38 "
            "(universe_writer_lock + PyPI summary guard), v0.39 "
            "(ssm_dispatcher diagnostics-write), v0.40 "
            "(artifact_freshness), v0.41 (build_batches_request_params), "
            "v0.42 (RegimeLiteral 3-class), v0.43 (transparency degraded "
            "status). The Pydantic V2 / 3.9 "
            "eval_type_backport conditional dep shipped in v0.32.0 still "
            "applies. Update this test if the pin moves further forward."
        )
