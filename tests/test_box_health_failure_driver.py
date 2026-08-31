"""box_health.sh names the DRIVER of a failure, and never guesses one.

Every test here fails against the code as it stood before this file existed:
the message carried `(last run result=exit-code)` and nothing else, and there
was no attribution function to interrogate.

The two properties under test are separable and both matter:

  * the message carries a determined cause, not an instruction to go and read a
    journal the emitter has already read;
  * a cause nobody anticipated lands in an EXPLICIT `unattributed` branch. An
    `elif` chain ending in a plausible default ("exited-nonzero") silently
    misattributes every unknown case while reading as an answer, which is worse
    than saying nothing.
"""

from __future__ import annotations

import pytest

from tests.box_health_helpers import (
    classify,
    partition,
    run_pure,
    timer_staleness_findings,
)


def driver(result: str = "exit-code", status: str = "", journal: str = "") -> str:
    return run_pure("timer_failure_driver", result, status, journal)


# ── the vocabulary is CLOSED and ordered most-specific-first ────────────────

@pytest.mark.parametrize(
    "result,status,journal,expected",
    [
        # An OOM kill is a SIGKILL. It is checked before the generic signal
        # branch precisely so the specific answer wins.
        ("oom-kill", "", "", "oom-killed"),
        ("signal", "", "Out of memory: Killed process 1234", "oom-killed"),
        # systemd-reserved statuses are exact, never ranges.
        ("exit-code", "217", "anything at all", "identity-unresolvable"),
        ("exit-code", "216", "anything at all", "identity-unresolvable"),
        ("exit-code", "203", "anything at all", "exec-missing"),
        ("exit-code", "200", "anything at all", "working-directory-missing"),
        ("exit-code", "226", "anything at all", "sandbox-denied"),
        # A cause named in the run's own journal.
        ("exit-code", "2",
         "cannot import name 'routes' from 'nousergon_lib.egress'",
         "import-or-dependency-broken"),
        ("exit-code", "1", "botocore ExpiredToken: the token expired",
         "credentials-expired"),
        ("exit-code", "1", "An error occurred (AccessDenied) when calling",
         "access-denied"),
        ("exit-code", "1", "OSError: [Errno 28] No space left on device",
         "disk-full"),
        ("exit-code", "1", "curl: (6) Could not resolve host: example.invalid",
         "upstream-unreachable"),
        ("exit-code", "1", "Dependency failed for something.service",
         "dependency-failed"),
        # Mechanism, only once no cause was named.
        ("timeout", "", "nothing recognisable here", "timed-out"),
        ("signal", "", "nothing recognisable here", "killed-by-signal"),
        ("start-limit-hit", "", "nothing recognisable here", "start-limit-hit"),
    ],
)
def test_each_vocabulary_row_is_reachable(result, status, journal, expected):
    assert driver(result, status, journal) == expected


def test_a_cause_in_the_journal_outranks_the_mechanism():
    """`start-limit-hit` is a CONSEQUENCE of earlier failures, never the answer
    when the run's own journal still names why it failed.

    This is the ordering that decides whether the operator is told "systemd
    gave up restarting it" or "it cannot import a module". Measured on
    llm-capability-probe.service 2026-08-31: both facts were true at once.
    """
    assert driver(
        "start-limit-hit", "2",
        "ModuleNotFoundError: No module named 'nousergon_lib.egress.routes'\n"
        "Start request repeated too quickly",
    ) == "import-or-dependency-broken"


# ── the terminal branches are EXPLICIT, and there are two of them ───────────

def test_an_unknown_cause_lands_in_unattributed_not_a_default():
    """The load-bearing negative test.

    A journal exists, it says something, and nothing in the vocabulary matches.
    The honest answer is `unattributed`. Any plausible-sounding label here --
    `exited-nonzero`, `unknown-error`, `application-error` -- would be an
    assertion the emitter cannot support, applied to every case nobody
    anticipated.
    """
    assert driver(
        "exit-code", "7",
        "widget frobnicator returned a value the operator has never seen",
    ) == "unattributed"


def test_a_failure_that_left_no_journal_record_is_its_own_named_outcome():
    """Real, and not an edge case.

    morning-signal-bakeoff.service reports Result=exit-code with `-- No
    entries --` over 30 days (measured live on the box, 2026-08-31). "The unit
    failed and left no evidence" is a different finding from "the unit failed
    and we could not classify the evidence", and collapsing them would hide a
    unit whose logging is broken behind a vocabulary gap.
    """
    assert driver("exit-code", "1", "") == "unattributed-no-journal-record"


def test_whitespace_only_journal_counts_as_no_record():
    """`journalctl -o cat --since <t>` can return a bare newline when the
    window lands past every entry. Treating that as a journal we failed to
    attribute would report the wrong one of the two terminal findings."""
    assert driver("exit-code", "1", "\n  \n") == "unattributed-no-journal-record"


def test_the_vocabulary_never_returns_an_empty_label():
    """A blank driver would render as a dangling `driver=` in the alert."""
    for args in (("", "", ""), ("success", "", ""), ("exit-code", "", "")):
        assert run_pure("timer_failure_driver", *args) != ""


# ── the MESSAGE carries it ─────────────────────────────────────────────────

def test_the_alert_line_carries_the_driver_and_not_a_journal_pointer():
    lines = timer_staleness_findings(
        "llm-capability-probe.timer",
        result="exit-code",
        fail_since="Mon 2026-08-31 07:01:39 UTC",
        driver="import-or-dependency-broken",
    )
    failing = [ln for ln in lines if ln.startswith("timer job failing: ")]
    assert failing, f"no failure finding emitted: {lines}"
    assert "driver=import-or-dependency-broken" in failing[0]
    assert "detail in journal" not in failing[0]


def test_the_driver_precedes_the_timestamps_in_the_line():
    """The only actionable part of the line is not buried behind two calendar
    strings."""
    line = [
        ln for ln in timer_staleness_findings(
            "x.timer", result="exit-code",
            fail_since="Mon 2026-08-31 07:01:39 UTC",
            next_elapse="Tue 2026-09-01 07:00:00 UTC",
            driver="oom-killed",
        ) if ln.startswith("timer job failing: ")
    ][0]
    assert line.index("driver=") < line.index("failing run started")


def test_a_missing_driver_is_named_rather_than_omitted():
    """An absent field would silently shorten the line, and box_health's
    confirm-on-retry intersection matches problem lines byte-for-byte -- a
    line whose length depends on whether a helper ran is a finding that can
    intermittently fail to confirm."""
    line = [
        ln for ln in timer_staleness_findings("x.timer", result="exit-code", driver="")
        if ln.startswith("timer job failing: ")
    ][0]
    assert "driver=unclassified" in line


def test_a_healthy_timer_still_emits_no_finding():
    """Attribution must not have turned a success into a problem line."""
    assert timer_staleness_findings(
        "x.timer", result="success", driver="none",
    ) == []


# ── the failed-unit backstop ───────────────────────────────────────────────

def test_a_unit_no_enumeration_reaches_is_reported():
    assert run_pure(
        "unit_is_covered",
        "llm-capability-probe.service",
        "dashboard.service nous-ergon-live.service",
        "morning-signal.service",
    ) == "no"


def test_a_manifested_service_is_covered():
    assert run_pure(
        "unit_is_covered", "dashboard.service",
        "dashboard.service nous-ergon-live.service", "",
    ) == "yes"


def test_a_service_reached_by_an_enabled_timer_is_covered():
    assert run_pure(
        "unit_is_covered", "morning-signal.service", "dashboard.service",
        "morning-signal.service ops-config-drift.service",
    ) == "yes"


def test_coverage_matches_whole_units_not_substrings():
    """`signal.service` must not be read as covering `morning-signal.service`.

    A substring match here would be a coverage check that quietly over-claims,
    which is the one thing a coverage check may never do."""
    assert run_pure(
        "unit_is_covered", "morning-signal.service", "signal.service", "",
    ) == "no"


def test_the_backstop_finding_is_critical():
    """It names a unit systemd itself calls failed. Every other coverage
    statement in this file is `watchdog: ` -> warning because it is about the
    watchdog's own bookkeeping (overseer-policy.md invariant 17); this one is
    about an outage."""
    assert classify(
        "unit failed and otherwise unmonitored: llm-capability-probe.service "
        "(last run result=exit-code, driver=import-or-dependency-broken)"
    ) == "critical"


def test_the_backstop_finding_takes_the_episode_keyed_publish():
    """Routed with `timer job failing:`, not into the set-derived critical
    tier -- otherwise a standing failed unit re-pages once per dedup window,
    which is the defect the episode key exists to remove."""
    line = (
        "unit failed and otherwise unmonitored: llm-capability-probe.service "
        "(last run result=exit-code, driver=import-or-dependency-broken)"
    )
    assert line in partition([line])["criticals"]

    import re

    from tests.box_health_helpers import BOX_HEALTH

    m = re.search(
        r'case "\$_line" in\n(.*?)\n    esac', BOX_HEALTH, re.DOTALL
    )
    assert m, "the timer/other critical partition was not found"
    block = m.group(1)
    assert "unit failed and otherwise unmonitored: " in block, (
        "the backstop finding is not routed into timer_criticals, so it will "
        "take the set-derived key and re-page on the general critical window."
    )
