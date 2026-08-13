"""Consumer contract: the Director plan page carries the run's correctness verdict.

`sf-pipeline-policy.md` §2.3a rule 3 — *every surface presenting the run's
results carries the verdict state.* A weekly action plan is the run's results
turned into proposals, and this page is the one an operator is most likely to
act from: it lists what the Director says to do about the week. Rendering the
proposals without the verdict asserts a guarantee nobody established, in the
place it costs the most.

**Producer/consumer contract.** The producer is ``crucible-evaluator
director/verdict.py::stamp_plan_artifact``, which stamps ``attestation``,
``advisory_unverified`` and ``actions_withheld`` onto
``director/{run_date}/action_plan.json``. It stamps them onto the serialized
body rather than onto ``DirectorWeeklyActionPlan``, because that model is the
LLM's structured-output schema — a verdict produced by the thing being verified
is not a verdict. This file pins the shape this consumer reads.

**The absent case is the deliverable.** A plan written before the Director
consumed the verdict, and a plan from a cycle whose verdict producer died, both
arrive with no ``attestation`` key and are otherwise indistinguishable from a
fully-attested one. Both must render UNKNOWN. The PASS case is the control.
"""

from unittest.mock import MagicMock

import pytest

from components import director_plan

_AS_OF = {"backtester": "2026-08-12T09:41:02Z",
          "evaluator_stage": "2026-08-12T10:02:55Z"}


def _plan(attestation=..., **extra) -> dict:
    """A minimal plan. ``attestation=...`` (the default) omits the block —
    the producer-never-consumed / producer-never-ran case."""
    plan = {
        "run_date": "2026-08-12",
        "system_summary": "The engine is fine.",
        "top_risks": ["drawdown"],
        "action_items": [],
    }
    if attestation is not ...:
        plan["attestation"] = attestation
    plan.update(extra)
    return plan


def _texts(mock_method) -> str:
    return " ".join(str(c.args[0]) for c in mock_method.call_args_list if c.args)


@pytest.fixture(autouse=True)
def st_mock():
    director_plan.st.reset_mock()
    director_plan.st.columns.side_effect = lambda spec: [
        MagicMock() for _ in (range(spec) if isinstance(spec, int) else spec)
    ]
    yield director_plan.st


class TestAbsenceIsNeverAPass:
    def test_plan_with_no_attestation_renders_unknown(self, st_mock):
        assert director_plan.render_plan_attestation(_plan()) == "UNKNOWN"
        text = _texts(st_mock.warning)
        assert "UNKNOWN" in text
        assert "Absence of evidence is never a pass" in text

    @pytest.mark.parametrize("bad", [None, "PASS", 1, [], {"verdict": "ok"},
                                     {"verdict": ""}, {}])
    def test_degenerate_blocks_render_unknown(self, bad):
        # Every one of these passes a `!= "FAIL"` or truthiness check.
        assert director_plan.render_plan_attestation(_plan(bad)) == "UNKNOWN"

    def test_no_plan_at_all_is_unknown(self):
        assert director_plan.render_plan_attestation(None) == "UNKNOWN"


class TestWithholdingIsNamed:
    def test_the_withheld_actions_are_listed(self, st_mock):
        director_plan.render_plan_attestation(_plan(
            {"verdict": "UNKNOWN", "as_of": _AS_OF, "reason": "backtester attestation absent."},
            advisory_unverified=True,
            actions_withheld=["issue_filing", "loop_verification"],
        ))
        text = _texts(st_mock.warning)
        assert "`issue_filing`" in text and "`loop_verification`" in text
        assert "nothing below was filed, reopened or escalated" in text
        assert "backtester attestation absent." in text

    def test_fail_uses_the_error_channel_and_says_wrong_not_unverified(self, st_mock):
        verdict = director_plan.render_plan_attestation(_plan(
            {"verdict": "FAIL", "as_of": _AS_OF, "reason": "a known-answer check disagreed."},
            actions_withheld=["issue_filing"],
        ))
        assert verdict == "FAIL"
        text = _texts(st_mock.error)
        assert "WRONG, not merely unverified" in text
        assert not _texts(st_mock.warning)


class TestPassIsTheControl:
    def test_pass_renders_success_with_the_as_of(self, st_mock):
        verdict = director_plan.render_plan_attestation(
            _plan({"verdict": "PASS", "as_of": _AS_OF}, advisory_unverified=False)
        )
        assert verdict == "PASS"
        text = _texts(st_mock.success)
        assert "PASS" in text
        # A verdict with no timestamp cannot read as stale.
        assert "2026-08-12T09:41:02Z" in text


class TestOrdering:
    def test_the_verdict_precedes_the_plan_on_the_overview(self, st_mock):
        # A proposal rendered before the verdict has already asserted the
        # guarantee — the reader has seen it by the time the qualifier arrives.
        director_plan.render_overview(_plan())
        calls = [c for c in st_mock.mock_calls if c[0] in ("warning", "caption", "markdown")]
        kinds = [c[0] for c in calls]
        assert kinds and kinds[0] == "warning", (
            f"the verdict must be the first element rendered, got {kinds[:3]}"
        )
