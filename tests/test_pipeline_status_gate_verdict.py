"""
tests/test_pipeline_status_gate_verdict.py — Unit tests for the gate-verdict
axis added to loaders/pipeline_status_loader.py (alpha-engine-config-I7313).

COMPLETE (derive_cycle_verdict) and VERIFIED (GateVerdict, here) are two
separate axes on the pipeline-status page. This module's central contract,
per the issue's deliverable 3: absence of the verdict data renders as
UNKNOWN, never as VERIFIED. Covers:

  - All 5 families reported, none fired -> VERIFIED
  - Any family fired true -> DEGRADED, regardless of what else is unreported
  - Any family absent (never present-and-false in production, per the
    2026-08-14 live sample) -> NOT_VERIFIED, never VERIFIED
  - No execution output at all (RUNNING execution, or a FAILED execution --
    DescribeExecution never populates ``output`` for a non-SUCCEEDED run) ->
    NOT_VERIFIED
  - Malformed / non-JSON / non-dict output -> NOT_VERIFIED, never raises
  - A non-terminal run status short-circuits before any read -> NOT_VERIFIED
  - No execution_arn at all -> NOT_VERIFIED
  - Any read failure (IAM denial, boto3 exception) -> NOT_VERIFIED, never
    silently swallowed into VERIFIED and never propagates to the caller

All boto3 calls mocked; no live AWS / network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nousergon_lib.pipeline_status import RunStatus

from loaders.pipeline_status_loader import (
    GateVerdict,
    GateVerdictResult,
    _gate_verdict_from_execution_output,
    read_gate_verdict_with_fallback,
)


SAT_ARN = (
    "arn:aws:states:us-east-1:711398986525:stateMachine:ne-weekly-freshness-pipeline"
)
EXEC_ARN = (
    "arn:aws:states:us-east-1:711398986525:execution:ne-weekly-freshness-pipeline:test-run-1"
)

_ALL_FALSE = {
    "gate_degraded": False,
    "health_check_degraded": False,
    "report_card_degraded": False,
    "parity_degraded": False,
    "research_predictor_degraded": False,
}


# ── _gate_verdict_from_execution_output (pure function) ───────────────────


def test_all_five_present_and_false_is_verified():
    result = _gate_verdict_from_execution_output(json.dumps(_ALL_FALSE))
    assert result.verdict == GateVerdict.VERIFIED
    assert result.degraded_families == ()
    assert result.unreported_families == ()


def test_one_family_fired_true_is_degraded():
    payload = dict(_ALL_FALSE, gate_degraded=True)
    result = _gate_verdict_from_execution_output(json.dumps(payload))
    assert result.verdict == GateVerdict.DEGRADED
    assert result.degraded_families == ("gate_degraded",)


def test_fired_family_wins_over_other_unreported_families():
    """Mirrors CheckGateDegradedNotify's most-specific-first ordering: a
    fired flag always routes to a degraded notifier regardless of what
    else the SF omitted."""
    payload = {"parity_degraded": True}  # everything else absent
    result = _gate_verdict_from_execution_output(json.dumps(payload))
    assert result.verdict == GateVerdict.DEGRADED
    assert result.degraded_families == ("parity_degraded",)


@pytest.mark.parametrize(
    "raw_output",
    [
        None,
        "",
        "not json {{{",
        json.dumps([1, 2, 3]),  # valid JSON, not an object
        json.dumps({}),  # valid object, all 5 families absent -- the
        # measured production shape (2026-08-14 live sample: 7/7 SUCCEEDED
        # executions of ne-weekly-freshness-pipeline carried none of the 5
        # flags)
        json.dumps({"gate_degraded": "not-a-bool"}),  # wrong type never reads as clean
    ],
)
def test_absence_or_malformed_output_never_renders_verified(raw_output):
    result = _gate_verdict_from_execution_output(raw_output)
    assert result.verdict != GateVerdict.VERIFIED
    assert result.verdict == GateVerdict.NOT_VERIFIED


def test_unreported_families_are_named():
    result = _gate_verdict_from_execution_output(json.dumps({"gate_degraded": False}))
    assert result.verdict == GateVerdict.NOT_VERIFIED
    assert set(result.unreported_families) == {
        "health_check_degraded",
        "report_card_degraded",
        "parity_degraded",
        "research_predictor_degraded",
    }


# ── read_gate_verdict_with_fallback (public loader) ────────────────────────


def test_no_execution_arn_is_not_verified():
    result = read_gate_verdict_with_fallback(SAT_ARN, None, RunStatus.SUCCEEDED)
    assert result.verdict == GateVerdict.NOT_VERIFIED
    assert result.error_message


@pytest.mark.parametrize(
    "status",
    [RunStatus.RUNNING, RunStatus.NOT_RUN],
)
def test_non_terminal_status_short_circuits_to_not_verified(status):
    with patch(
        "loaders.pipeline_status_loader._cached_gate_verdict_output"
    ) as mock_read:
        result = read_gate_verdict_with_fallback(SAT_ARN, EXEC_ARN, status)
    mock_read.assert_not_called()
    assert result.verdict == GateVerdict.NOT_VERIFIED


def test_terminal_status_with_clean_output_is_verified():
    with patch(
        "loaders.pipeline_status_loader._cached_gate_verdict_output",
        return_value=json.dumps(_ALL_FALSE),
    ):
        result = read_gate_verdict_with_fallback(SAT_ARN, EXEC_ARN, RunStatus.SUCCEEDED)
    assert result.verdict == GateVerdict.VERIFIED


def test_failed_run_with_no_output_is_not_verified():
    """FAILED executions never populate DescribeExecution's ``output`` --
    verified live 2026-08-14 against watch-rerun-2026-08-13-5."""
    with patch(
        "loaders.pipeline_status_loader._cached_gate_verdict_output",
        return_value=None,
    ):
        result = read_gate_verdict_with_fallback(SAT_ARN, EXEC_ARN, RunStatus.FAILED)
    assert result.verdict == GateVerdict.NOT_VERIFIED


def test_read_failure_is_not_verified_and_does_not_raise():
    with patch(
        "loaders.pipeline_status_loader._cached_gate_verdict_output",
        side_effect=Exception("states:DescribeExecution denied"),
    ):
        result = read_gate_verdict_with_fallback(SAT_ARN, EXEC_ARN, RunStatus.SUCCEEDED)
    assert result.verdict == GateVerdict.NOT_VERIFIED
    assert "DescribeExecution" in result.error_message or "Exception" in result.error_message


def test_degraded_run_reports_which_families():
    payload = dict(_ALL_FALSE, health_check_degraded=True, parity_degraded=True)
    with patch(
        "loaders.pipeline_status_loader._cached_gate_verdict_output",
        return_value=json.dumps(payload),
    ):
        result = read_gate_verdict_with_fallback(SAT_ARN, EXEC_ARN, RunStatus.SUCCEEDED)
    assert result.verdict == GateVerdict.DEGRADED
    assert set(result.degraded_families) == {"health_check_degraded", "parity_degraded"}
    assert "health_check_degraded" not in result.summary  # human label, not the raw key
    assert "tail health checks" in result.summary


# ── Summary text sanity (renders something distinct per verdict) ──────────


def test_summary_text_differs_across_verdicts():
    verified = GateVerdictResult(GateVerdict.VERIFIED)
    degraded = GateVerdictResult(GateVerdict.DEGRADED, degraded_families=("gate_degraded",))
    not_verified = GateVerdictResult(
        GateVerdict.NOT_VERIFIED, unreported_families=("gate_degraded",)
    )
    summaries = {verified.summary, degraded.summary, not_verified.summary}
    assert len(summaries) == 3  # all three visually/textually distinct
