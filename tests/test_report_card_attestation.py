"""Consumer contract: the Report Card surfaces carry the §2.3a correctness verdict.

`sf-pipeline-policy.md` §2.3a rule 3 — *every surface presenting the run's results
carries the verdict state*. The dashboard is the third such surface (the card JSON
and the Director digest are the other two, shipped in crucible-evaluator-PR187).

**Producer/consumer contract.** The producer is
``crucible-evaluator grading/attestation.py::build_run_attestation``
(``report_card_attestation-1.0.0``), which writes the block onto
``evaluator/{run_date}/report_card.json``. ``tests/fixtures/
report_card_attestation_pass.json`` pins the shape this consumer reads; a producer
change that renames ``verdict``, ``reason`` or the two half-blocks fails here rather
than silently rendering a blank tile in production (config-I6974).

**The case that matters is the MISSING one.** A card with no attestation block is
exactly what a cycle whose verdict producer never ran looks like, and the whole
point of §2.3a is that this must not be indistinguishable from a clean run. Every
absence assertion below is the real deliverable; the PASS case is the control.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from components import report_card_v2

FIXTURES = Path(__file__).parent / "fixtures"
PASS_BLOCK = json.loads((FIXTURES / "report_card_attestation_pass.json").read_text())


def _card(attestation=..., **extra) -> dict:
    """A minimal RC v2 card. ``attestation=...`` (the default) omits the block
    entirely — the producer-never-ran case."""
    card = {
        "tiles_overall_status": "GREEN",
        "tiles": {
            "portfolio_outcome": {
                "status": "GREEN", "letter": "A", "numeric_grade": 92.0,
                "components": [{"name": "total_alpha", "status": "GREEN",
                                "criticality": "critical", "value": 0.04}],
            },
        },
        "_provenance": {"run_date": "2026-08-12", "artifacts": {"n_read": 40, "n_missing": 2}},
    }
    if attestation is not ...:
        card["attestation"] = attestation
    card.update(extra)
    return card


def _texts(mock_method) -> str:
    return " ".join(str(c.args[0]) for c in mock_method.call_args_list if c.args)


@pytest.fixture(autouse=True)
def st_mock():
    report_card_v2.st.reset_mock()
    # `st.columns(n)` must yield n context managers; `st.container(...)` likewise.
    report_card_v2.st.columns.side_effect = lambda spec: [
        MagicMock() for _ in (range(spec) if isinstance(spec, int) else spec)
    ]
    yield report_card_v2.st


# ---------------------------------------------------------------------------
# verdict_is_pass — the truthiness trap
# ---------------------------------------------------------------------------

class TestVerdictIsPass:
    """Binding constraint from config-I6974: only the literal "PASS" is a pass.

    Mirrors ``crucible-evaluator grading/attestation.py::verdict_is_pass``. If this
    ever becomes ``bool(verdict)``, a producer that starts emitting ``"ok"`` — or a
    ``reason``-only degraded block — silently renders as verified.
    """

    def test_only_the_literal_pass_is_a_pass(self):
        assert report_card_v2.verdict_is_pass("PASS") is True

    @pytest.mark.parametrize("verdict", ["ok", "OK", "pass", "", "UNKNOWN", "FAIL", None, 1, True])
    def test_everything_else_withholds_the_guarantee(self, verdict):
        assert report_card_v2.verdict_is_pass(verdict) is False


class TestVerdictNormalization:
    def test_absent_block_is_unknown(self):
        assert report_card_v2._attestation_verdict(_card()) == "UNKNOWN"

    @pytest.mark.parametrize("block", [None, "PASS", [], 0, {"verdict": "ok"}, {}])
    def test_malformed_block_is_unknown_never_pass(self, block):
        assert report_card_v2._attestation_verdict(_card(attestation=block)) == "UNKNOWN"


# ---------------------------------------------------------------------------
# The three rendered cases
# ---------------------------------------------------------------------------

class TestRenderAttestation:
    def test_pass_renders_a_confirmation_naming_both_check_counts(self, st_mock):
        verdict = report_card_v2.render_attestation(_card(attestation=PASS_BLOCK))
        assert verdict == "PASS"
        assert st_mock.error.call_count == 0
        assert st_mock.warning.call_count == 0
        text = _texts(st_mock.success)
        assert "PASS" in text
        # Both halves' n_checks are named — the operator can see that the
        # backtester half was actually read, not defaulted.
        assert "evaluator 6" in text
        assert "backtester 5" in text

    def test_fail_renders_an_error_carrying_the_producer_reason(self, st_mock):
        block = {**PASS_BLOCK, "verdict": "FAIL",
                 "reason": "backtester attestation FAILED on 1 known-answer check(s): fee_charged_both_sides."}
        verdict = report_card_v2.render_attestation(_card(attestation=block))
        assert verdict == "FAIL"
        assert st_mock.success.call_count == 0
        text = _texts(st_mock.error)
        assert "FAIL" in text
        assert "fee_charged_both_sides" in text

    def test_missing_block_renders_a_warning_not_silence(self, st_mock):
        """The config-I6974 closes-when case. An absent block must SAY that
        nothing checked these numbers — never render as an absence of problems."""
        verdict = report_card_v2.render_attestation(_card())
        assert verdict == "UNKNOWN"
        assert st_mock.success.call_count == 0
        assert st_mock.warning.call_count == 1, "an absent verdict must produce a visible warning"
        text = _texts(st_mock.warning)
        assert "UNKNOWN" in text
        assert "NOT" in text and "correct" in text
        assert "never ran" in text

    def test_explicit_unknown_renders_the_same_withholding(self, st_mock):
        block = {**PASS_BLOCK, "verdict": "UNKNOWN",
                 "reason": "backtester attestation absent at s3://... — the producer never ran this cycle."}
        assert report_card_v2.render_attestation(_card(attestation=block)) == "UNKNOWN"
        assert st_mock.success.call_count == 0
        assert "UNKNOWN" in _texts(st_mock.warning)

    def test_unrecognised_verdict_string_is_not_a_pass(self, st_mock):
        assert report_card_v2.render_attestation(_card(attestation={"verdict": "ok"})) == "UNKNOWN"
        assert st_mock.success.call_count == 0
        assert st_mock.warning.call_count >= 1

    def test_no_card_renders_nothing_and_claims_nothing(self, st_mock):
        # No numbers on screen, so nothing to qualify — but it is still not a pass.
        assert report_card_v2.render_attestation(None) == "UNKNOWN"
        assert st_mock.success.call_count == 0


class TestStalenessFlags:
    """config#2885's degraded_staleness / stale_tiles have been emitted by the
    evaluator since 2026-06 and had never reached any rendering surface."""

    def test_degraded_staleness_is_surfaced_with_the_tile_names(self, st_mock):
        report_card_v2.render_attestation(
            _card(attestation=PASS_BLOCK, degraded_staleness=True,
                  stale_tiles=["predictor", "research"])
        )
        text = _texts(st_mock.warning)
        assert "stale" in text.lower()
        assert "predictor" in text and "research" in text

    def test_clean_card_raises_no_staleness_warning(self, st_mock):
        report_card_v2.render_attestation(_card(attestation=PASS_BLOCK))
        assert st_mock.warning.call_count == 0


# ---------------------------------------------------------------------------
# Every surface, not just the one that prompted the fix
# ---------------------------------------------------------------------------

class TestEverySurfaceCarriesIt:
    @pytest.mark.parametrize("renderer", ["render_overview", "render_detail", "render_home_summary"])
    def test_surface_warns_when_the_verdict_is_absent(self, st_mock, renderer):
        getattr(report_card_v2, renderer)(_card())
        assert st_mock.warning.call_count >= 1, (
            f"{renderer} rendered the run's numbers with no verdict state — §2.3a rule 3"
        )

    @pytest.mark.parametrize("renderer", ["render_overview", "render_detail", "render_home_summary"])
    def test_surface_confirms_when_attested(self, st_mock, renderer):
        getattr(report_card_v2, renderer)(_card(attestation=PASS_BLOCK))
        assert st_mock.success.call_count >= 1

    def test_overview_deemphasises_grades_when_unattested(self, st_mock):
        report_card_v2.render_overview(_card())
        captions = _texts(st_mock.caption)
        assert "UNVERIFIED" in captions.upper()
        assert ":gray[" in " ".join(
            str(c.args[0]) for c in st_mock.markdown.call_args_list if c.args
        ) + captions

    def test_overview_does_not_deemphasise_a_verified_run(self, st_mock):
        report_card_v2.render_overview(_card(attestation=PASS_BLOCK))
        assert "UNVERIFIED" not in _texts(st_mock.caption).upper()


# ---------------------------------------------------------------------------
# Producer/consumer contract on the fixture itself
# ---------------------------------------------------------------------------

class TestFixtureMatchesTheProducerContract:
    """Pins what crucible-evaluator writes. A producer rename fails HERE."""

    def test_schema_version_is_the_one_this_consumer_reads(self):
        assert PASS_BLOCK["schema"] == "report_card_attestation-1.0.0"

    @pytest.mark.parametrize("field", ["schema", "run_date", "verdict", "evaluator", "backtester", "reason"])
    def test_top_level_fields_present(self, field):
        assert field in PASS_BLOCK

    @pytest.mark.parametrize("half", ["evaluator", "backtester"])
    def test_each_half_carries_its_own_verdict_and_check_count(self, half):
        assert PASS_BLOCK[half]["verdict"] in {"PASS", "FAIL", "UNKNOWN"}
        assert isinstance(PASS_BLOCK[half]["n_checks"], int)

    def test_backtester_half_names_the_s3_source(self):
        # Rule 1 made concrete: the consumer can say WHICH key was read, so a
        # stale-cycle verdict is diagnosable from the rendered page.
        assert PASS_BLOCK["backtester"]["source_path"].endswith("/attestation.json")
        assert PASS_BLOCK["backtester"]["schema"].startswith("backtest_attestation-")
