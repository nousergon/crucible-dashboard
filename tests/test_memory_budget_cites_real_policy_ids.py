"""Every policy id `check_memory_budget.py` prints must exist, and mean what it says.

WHY (alpha-engine-config-I7803, measured 2026-08-20). The steady-state breach
line ended `"the box is genuinely too small for what it runs (policy T1-7 /
exit trigger E3)"`. E3's predicate in `shared-application-host-policy.md` §6 is
**sustained MemAvailable < 250 MB**, and its named successor is a t3.large
resize that T1-7 itself prices at ~$30/month of new, entirely uncovered spend
on a ~$62/month account.

At the moment that line printed on `i-09b539c844515d549`:

    working set   2150 MB  (56% of RAM — over the bound)
    MemAvailable  1219 MB  (4.9x E3's threshold)
    kernel OOM kills, 14 days: 0
    memory.pressure full total: 1.97 s across 15.4 h

So the checker asserted an exit condition it had not evaluated, on a healthy
box, pointing at the most expensive remedy in the policy. The bound itself was
real and correct — it simply had no id, because it was declared in
`budget.yaml` and enforced here while appearing in no policy. It is now §5
T1-8.

A detector quotes the rule it evaluated. Naming a policy id it did not test is
how a threshold nobody agreed to acquires the authority of one that was.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CHECKER = REPO_ROOT / "infrastructure" / "check_memory_budget.py"

# NO CROSS-REPO ASSERTION HERE, deliberately. An earlier draft resolved
# `shared-application-host-policy.md` out of a sibling `nous-ergon-ops`
# checkout to assert that T1-8 exists. `test_no_uncontrolled_sibling_checkout_
# paths.py` rejected it, correctly (alpha-engine-config-I7605): a test whose
# verdict depends on which branch a neighbouring working tree happens to be on
# is not testing the contract, it is testing the laptop.
#
# The cross-repo half already has an owner. `nous-ergon-ops` registers
# `SAH-5-T1-8-steady-state-working-set-bound` in
# `governance/policy-clauses.d/` with `kind: external` pointing at THIS file —
# that registry is the fleet's mechanism for "a policy clause is enforced
# somewhere else", and duplicating it here as a filesystem walk would be a
# second, weaker copy of a link that already exists.
#
# What stays here is what this repo can prove about itself: the line it emits.
class TestSteadyStateBreachLine:
    """The specific line that was wrong."""

    def _line(self) -> str:
        """The steady-state breach block with COMMENTS STRIPPED.

        The contract is about what the checker PRINTS. The block's own comment
        quotes the old wrong string verbatim as the record of what was fixed,
        and a naive substring search over the raw block matches that quotation
        rather than the emitted text."""
        src = CHECKER.read_text()
        i = src.index("if ss_over:")
        block = src[i : src.index("if tj_over:", i)]
        return "\n".join(
            line for line in block.splitlines() if not line.lstrip().startswith("#")
        )

    def test_cites_t1_8(self):
        assert "T1-8" in self._line(), (
            "the steady-state breach must cite the headroom invariant it "
            "actually evaluated"
        )

    def test_does_not_claim_an_exit_condition(self):
        line = self._line()
        # It may NAME E3 to disclaim it; it must not assert it.
        assert "exit trigger E3)" not in line
        assert "genuinely too small" not in line, (
            "'the box is genuinely too small for what it runs' is E3's "
            "conclusion, reached from a predicate this check never evaluates"
        )

    def test_disclaims_e3_explicitly(self):
        """Naming the distinction is what stops the conflation being re-made by
        the next reader of the output."""
        line = self._line()
        assert "NOT exit trigger" in line or "not exit trigger" in line.lower()
        assert "MemAvailable" in line, (
            "the disclaimer must state E3's actual predicate, or it is just an "
            "assertion that they differ"
        )

    def test_discloses_censoring_in_the_breach_line_itself(self):
        """A finding is routinely copied into an issue without the HYGIENE line
        above it, and a floor quoted as a measurement is worse than no number."""
        line = self._line()
        assert "FLOOR" in line or "floor" in line
        assert "censored" in line, (
            "the censored-unit count must be interpolated into the breach line, "
            "not left only in a separate hygiene line"
        )

    def test_names_the_remedy_that_matches_the_tier(self):
        """§5 remediations mean 'bring the box back inside its budget'. If the
        line tells the reader to resize, the tier citation is decorative."""
        line = self._line()
        assert "Lower a cap" in line or "move a service" in line
