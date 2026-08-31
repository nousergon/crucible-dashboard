"""One page per failure EPISODE, and an unreadable prior state pages anyway.

These pin properties that landed with crucible-dashboard#787 and were never
covered end-to-end: #787's own tests exercise `timer_failure_episode_key` as a
pure function, which cannot see whether the SHIPPED publish path actually
carries the carried key onto the wire, whether a recovery lets the next failure
open a fresh episode, or what happens when the state file cannot be read.

Measured provenance, so a later reader can tell what these are protecting.
From the live krepis dedup markers under `s3://alpha-engine-research/_alerts/
_dedup/` (633 objects, read 2026-08-31):

  * `boxhealth-critical-timer_job_failing:_morning-signal-bakeoff.timer_(last
    _run_result` — publish_count **78**, 2026-08-05 .. 2026-08-12. The
    SET-DERIVED key, from before the identity key existed: one condition,
    78 deliveries, roughly one every two hours for a week.
  * `boxhealth-critical-timerfail-morning-signal-bakeoff.timer-exit-code-
    1787886059` — publish_count **1**, 2026-08-28. The same unit, the same
    fault, under the identity key.

The mechanism that produced the 78 is gone. What is NOT covered anywhere is
that it stays gone through the publish path, which is what this file asserts.
"""

from __future__ import annotations

import shlex

from tests.box_health_helpers import run_lifecycle

UNIT = "metron-deploy-drift.timer"
FINDING = "timer job failing: metron-deploy-drift.timer (last run result=exit-code, driver=upstream-unreachable)"


def _tick(prior_rows: str, result: str, ts: str) -> str:
    """One box-health tick: seed the prior state, publish, write the new state.

    Deliberately the SHIPPED sequence -- episode key, publish_problems,
    publish_clears, alerted_state_write -- rather than a re-implementation, for
    the same reason the rest of this harness runs bash.
    """
    return "\n".join([
        f'printf %s {shlex.quote(prior_rows)} > "$ALERTED_STATE"',
        f'_key=$(timer_failure_episode_key "{UNIT}" "{result}" "{ts}" '
        '"$(alerted_state_prior | cut -f1)")',
        f'publish_problems critical 43200 "health alert" {shlex.quote(FINDING)} "$_key"',
        'ALERTED_NOW="${ALERTED_NOW%$\'\\n\'}"',
        'publish_clears "$(alerted_state_prior)" "$ALERTED_NOW"',
        'alerted_state_write "$ALERTED_NOW"',
        'printf "KEY=%s\\n" "$_key"',
        'printf "STATE=%s\\n" "$(cat "$ALERTED_STATE")"',
    ])


def _key_of(run) -> str:
    for ln in run.proc.stdout.splitlines():
        if ln.startswith("KEY="):
            return ln[4:]
    raise AssertionError(f"no key emitted: {run.proc.stdout}\n{run.proc.stderr}")


def _state_of(run) -> str:
    for ln in run.proc.stdout.splitlines():
        if ln.startswith("STATE="):
            return ln[6:]
    raise AssertionError("no state emitted")


FIRST_KEY = f"boxhealth-critical-timerfail-{UNIT}-exit-code-1787864827"
# A REAL tab: alerted_state rows are tab-separated and `cut -f1` is what
# reads them. A repr-escaped "\\t" would make the whole row read as the
# key and the harness would prove nothing.
ROW = f"{FIRST_KEY}\tcritical\t{FINDING}"


class TestOneEpisodeOnePage:
    def test_a_later_occurrence_of_one_unbroken_episode_reuses_the_first_key(self):
        """The occurrence that matters: an HOURLY timer failing all night.

        Its InactiveExitTimestamp advances on every fire, so a key built from
        that timestamp mints a new page every hour -- five CRITICALs and five
        RESOLVEDs in five hours for one undeployed commit, measured on this box
        2026-08-27/28. The episode is "this unit is failing"; it opened once.
        """
        run = run_lifecycle(_tick(ROW, "exit-code", "Fri 2026-08-28 01:07:38 UTC"),
                            __import__("pathlib").Path(_tmp()))
        assert _key_of(run).endswith("-1787864827"), (
            "the second occurrence minted a new key from its own exit "
            f"timestamp: {_key_of(run)}"
        )

    def test_the_carried_page_is_still_marked_still_open_not_reopened(self):
        run = run_lifecycle(_tick(ROW, "exit-code", "Fri 2026-08-28 01:07:38 UTC"),
                            __import__("pathlib").Path(_tmp()))
        key = _key_of(run)
        assert run.page_state(key) == "still_open"

    def test_no_clear_is_emitted_while_the_episode_stands(self):
        """A RESOLVED for a condition that has not ended is worse than the
        duplicate page: it tells the operator the thing is fixed."""
        run = run_lifecycle(_tick(ROW, "exit-code", "Fri 2026-08-28 01:07:38 UTC"),
                            __import__("pathlib").Path(_tmp()))
        assert run.channel_clears == {}


class TestRecoveryReopens:
    def test_a_recovery_clears_and_the_next_failure_pages_immediately(self):
        """Two properties in one run, because they are one property: the
        episode has to END for the next one to be able to begin.

        No time passes here and no window is waited out -- a genuinely new
        episode pages on the very next tick, which is what distinguishes
        episode keying from a suppression window.
        """
        tmp = __import__("pathlib").Path(_tmp())
        recovered = "\n".join([
            f'printf %s {shlex.quote(ROW)} > "$ALERTED_STATE"',
            # The unit succeeded: nothing found, so ALERTED_NOW stays empty.
            'ALERTED_NOW=""',
            'publish_clears "$(alerted_state_prior)" "$ALERTED_NOW"',
            'alerted_state_write "$ALERTED_NOW"',
            # ... and it fails again, with nothing in the prior rows to carry.
            f'_key=$(timer_failure_episode_key "{UNIT}" "exit-code" '
            '"Fri 2026-08-28 06:07:38 UTC" "$(alerted_state_prior | cut -f1)")',
            f'publish_problems critical 43200 "health alert" {shlex.quote(FINDING)} "$_key"',
            'printf "KEY=%s\\n" "$_key"',
            'printf "STATE=%s\\n" "$(cat "$ALERTED_STATE")"',
        ])
        run = run_lifecycle(recovered, tmp)
        assert FIRST_KEY in run.channel_clears, "the recovery emitted no terminator"
        key = _key_of(run)
        assert not key.endswith("-1787864827"), (
            "the new failure carried the CLEARED episode's key forward, so the "
            f"operator gets no page for a genuinely new outage: {key}"
        )
        assert key in run.channel_pages, "the new episode did not page"
        assert run.page_state(key) == "opened"


class TestUnreadableStateFailsOpen:
    def test_an_unreadable_prior_state_pages_rather_than_swallowing(self):
        """`alerted_state_prior` returns empty when the file cannot be read --
        and empty must mean "nothing was alerted", never "everything is
        already covered".

        Fail OPEN is the only safe direction: a duplicate page costs one
        message, a swallowed one costs the outage. This is the same rule the
        cgroup harm gate follows (an unreadable stall reading REPORTS) and the
        one this file's recurring defect keeps violating -- a check whose
        silent path is also its broken path.
        """
        tmp = __import__("pathlib").Path(_tmp())
        body = "\n".join([
            # Not merely absent: present and unreadable, which is the case a
            # `[ -r ]` guard and a missing file do NOT exercise the same way.
            'printf %s "unreadable" > "$ALERTED_STATE"',
            'chmod 000 "$ALERTED_STATE"',
            f'_key=$(timer_failure_episode_key "{UNIT}" "exit-code" '
            '"Fri 2026-08-28 01:07:38 UTC" "$(alerted_state_prior | cut -f1)")',
            f'publish_problems critical 43200 "health alert" {shlex.quote(FINDING)} "$_key"',
            'chmod 644 "$ALERTED_STATE"',
            'printf "KEY=%s\\n" "$_key"',
            'printf "STATE=%s\\n" ""',
        ])
        run = run_lifecycle(body, tmp)
        key = _key_of(run)
        assert key in run.channel_pages, (
            "an unreadable prior state produced no page -- the watchdog failed "
            "CLOSED, and a condition it could not remember became a condition "
            "nobody is told about."
        )
        assert run.page_state(key) == "opened"


_TMPS: list = []


def _tmp() -> str:
    import tempfile
    d = tempfile.mkdtemp(prefix="boxhealth-episode-")
    _TMPS.append(d)
    return d
