"""Confirm-on-retry survives a run that finishes MID-WINDOW.

Measured 2026-09-06 on i-09b539c844515d549 (journal of box-health.service).
box-health.timer is `OnUnitActiveSec=10min`, so its ticks drift against the
wall clock; metron-deploy-drift.timer fires hourly at :07 and its service runs
~6-13s. The tick starting 12:07:21 UTC therefore straddled the run:

  12:07:34  sample 1 — the service was ActiveState=activating, so `Result` read
            `success` and classify_timer_staleness re-emitted the PRIOR finding
            verbatim (the I8359 carry): `... failing run started 11:07:00 ...`
  12:07:48  sample 2 \\
  12:08:05  sample 3  > the run had finished and failed:
  12:08:20  sample 4 /  `... failing run started 12:07:21 ...`

The intersection was `comm -12` over the full byte string, so neither line was
in the other's sample and the confirmed set emptied. At 12:08:33 the box sent
`alerts.clear identity_key=boxhealth-critical-timerfail-metron-deploy-drift
.timer-exit-code-1788671220` — a RESOLVED for a condition nobody had touched —
and the key left ALERTED_STATE. The 12:18 tick, all four samples agreeing
again, found no prior key to carry and opened a NEW episode: a fresh CRITICAL.
Brian received that pair twice on 2026-09-06, at 05:07 and 12:07 UTC.

The class is not one timer: the confirmation window is ~12s wide and drifts, so
any hourly timer with a few-second service lands a run inside it a couple of
times a day. #777/I8359's carry covers only the case where all four samples
fall INSIDE the run.

Everything below runs the SHIPPED bash — the pure helpers and the main-flow
publish loop, extracted by anchor — for the reason box_health_helpers.py's own
docstring gives: a Python transcription proves only that two implementations
agreed on the day someone wrote them.
"""

from __future__ import annotations

import shlex

from tests.box_health_helpers import (
    install_fake_systemctl,
    run_lifecycle,
    timer_publish_loop_source,
)

UNIT = "metron-deploy-drift.timer"
SVC = "metron-deploy-drift.service"

# The two texts, verbatim in shape from the journal: same unit, same Result,
# different failing-run timestamp.
S1 = (
    f"timer job failing: {UNIT} (last run result=exit-code, "
    "driver=upstream-unreachable, failing run started Sat 2026-09-06 11:07:00 UTC, "
    "next attempt Sat 2026-09-06 12:07:00 UTC)"
)
S2 = (
    f"timer job failing: {UNIT} (last run result=exit-code, "
    "driver=upstream-unreachable, failing run started Sat 2026-09-06 12:07:21 UTC, "
    "next attempt Sat 2026-09-06 13:07:00 UTC)"
)

# The key the 11:07 run was published under — the one the 12:08:33 clear
# named. Carries `-upstream-unreachable-` (alpha-engine-config-I10237): the
# episode prefix folds in the driver S1/S2 already declare in their own text,
# so the carried key must match that shape or this fixture stops testing the
# mid-window intersection and starts testing I10237's carry instead.
PRIOR_KEY = f"boxhealth-critical-timerfail-{UNIT}-exit-code-upstream-unreachable-1788671220"
PRIOR_ROW = f"{PRIOR_KEY}\tcritical\t{S1}"


def _emit(run, marker: str) -> str:
    for ln in run.proc.stdout.splitlines():
        if ln.startswith(f"{marker}="):
            return ln[len(marker) + 1:]
    raise AssertionError(
        f"no {marker} emitted.\nstdout: {run.proc.stdout}\nstderr: {run.proc.stderr}"
    )


def _intersect(tmp_path, running: list[str], nxt: list[str]) -> list[str]:
    """Run the SHIPPED confirm_intersect on one pair of samples."""
    body = "\n".join([
        f'_out=$(confirm_intersect {shlex.quote(chr(10).join(running))} '
        f'{shlex.quote(chr(10).join(nxt))})',
        r'printf "OUT=%s\n" "$(printf %s "$_out" | tr "\n" "\037")"',
    ])
    run = run_lifecycle(body, tmp_path)
    raw = _emit(run, "OUT")
    return [x for x in raw.split("\x1f") if x]


def _window(tmp_path, samples: list[list[str]]) -> list[str]:
    """Fold the shipped intersection across a whole confirmation window."""
    confirmed = samples[0]
    for nxt in samples[1:]:
        confirmed = _intersect(tmp_path, confirmed, nxt)
    return confirmed


class TestIdentityIntersection:
    def test_a_timer_finding_whose_detail_moved_mid_window_is_still_confirmed(
        self, tmp_path
    ):
        """The 12:07 window, on the intersection alone."""
        assert _window(tmp_path, [[S1], [S2], [S2], [S2]]) == [S2]

    def test_the_confirmed_line_is_the_LAST_sample_not_the_first(self, tmp_path):
        """The published page must name the newest failing run.

        Carrying sample 1's text forward would page about an 11:07 run while
        the 12:07 one is the live fault — correct on the key, wrong on the
        evidence, and the evidence is the whole reason the line carries a
        timestamp (I7677).
        """
        out = _window(tmp_path, [[S1], [S2], [S2], [S2]])
        assert out == [S2] and "12:07:21" in out[0]

    def test_two_different_units_do_not_confirm_each_other(self, tmp_path):
        """Widening to the unit must not widen past it."""
        other = S2.replace(UNIT, "metron-refresh.timer")
        assert _window(tmp_path, [[S1], [other]]) == []

    def test_a_backstop_unit_failure_line_gets_the_same_identity(self, tmp_path):
        a = ("unit failed and otherwise unmonitored: foo.service "
             "(last run result=exit-code, driver=oom, failing run started A)")
        b = ("unit failed and otherwise unmonitored: foo.service "
             "(last run result=exit-code, driver=oom, failing run started B)")
        assert _window(tmp_path, [[a], [b]]) == [b]


class TestEverythingElseStaysExact:
    def test_a_non_timer_line_still_needs_byte_agreement(self, tmp_path):
        """The strictness is only relaxed where a detail legitimately moves.

        Two different ports are two different problems; if this ever confirmed
        one from the other, the truncated-`ss` false-positive class the
        confirm-on-retry window exists to kill would be back.
        """
        assert _window(tmp_path, [["port not listening: 8501"],
                                  ["port not listening: 8502"]]) == []

    def test_an_identical_non_timer_line_survives(self, tmp_path):
        assert _window(tmp_path, [["port not listening: 8501"],
                                  ["port not listening: 8501"]]) == \
            ["port not listening: 8501"]


def _publish_tick(prior_rows: str, findings: list[str]) -> str:
    """One tick's timer-finding publish, through the SHIPPED main-flow loop."""
    return "\n".join([
        f'printf %s {shlex.quote(prior_rows)} > "$ALERTED_STATE"',
        f'timer_criticals={shlex.quote(chr(10).join(findings))}',
        timer_publish_loop_source(),
        "ALERTED_NOW=\"${ALERTED_NOW%$'\\n'}\"",
        'publish_clears "$(alerted_state_prior)" "$ALERTED_NOW"',
        'alerted_state_write "$ALERTED_NOW"',
        'printf "KEY=%s\\n" "$_tf_key"',
    ])


class TestPublishTimeInFlight:
    """`Result` is re-read LIVE at publish time, minutes after the window."""

    def _run(self, tmp_path, active_state: str, result: str, ts: str):
        install_fake_systemctl(tmp_path)
        return run_lifecycle(
            _publish_tick(PRIOR_ROW, [S2]),
            tmp_path,
            overrides={
                "FAKE_SYSTEMCTL_Unit": SVC,
                "FAKE_SYSTEMCTL_ActiveState": active_state,
                "FAKE_SYSTEMCTL_Result": result,
                "FAKE_SYSTEMCTL_InactiveExitTimestamp": ts,
            },
        )

    def test_an_in_flight_unit_carries_the_prior_key_instead_of_re_deriving(
        self, tmp_path
    ):
        """systemd has already reset Result to `success` for a running unit, so
        re-deriving here builds a `...-success-` prefix that matches no prior
        key and opens a spurious episode."""
        run = self._run(tmp_path, "activating", "success", "")
        assert _emit(run, "KEY") == PRIOR_KEY

    def test_an_in_flight_unit_emits_no_clear_and_no_new_episode(self, tmp_path):
        run = self._run(tmp_path, "activating", "success", "")
        assert run.channel_clears == {}
        assert run.page_state(PRIOR_KEY) == "still_open"

    def test_a_settled_unit_is_still_read_live(self, tmp_path):
        """The carry is scoped to in-flight. A finished unit's real outcome is
        what the episode key must be built from — otherwise a Result CHANGE
        (`exit-code` becoming `timeout`, a different fault) could never open the
        new episode I7677/#787 require."""
        run = self._run(tmp_path, "inactive", "timeout", "Sat 2026-09-06 12:07:30 UTC")
        assert _emit(run, "KEY").startswith(
            f"boxhealth-critical-timerfail-{UNIT}-timeout-"
        )


class TestTheMeasuredTickEndToEnd:
    """The 12:07:21 tick, window and publish together, on the shipped code."""

    def test_zero_clears_and_zero_new_episodes(self, tmp_path):
        confirmed = _window(tmp_path, [[S1], [S2], [S2], [S2]])
        assert confirmed == [S2], "the window emptied again"

        install_fake_systemctl(tmp_path)
        run = run_lifecycle(
            _publish_tick(PRIOR_ROW, confirmed),
            tmp_path,
            overrides={
                "FAKE_SYSTEMCTL_Unit": SVC,
                # By publish time the 12:07:21 run has finished and failed.
                "FAKE_SYSTEMCTL_ActiveState": "inactive",
                "FAKE_SYSTEMCTL_Result": "exit-code",
                "FAKE_SYSTEMCTL_InactiveExitTimestamp": "Sat 2026-09-06 12:07:21 UTC",
            },
        )
        assert run.channel_clears == {}, (
            "a RESOLVED was sent for a condition that never ended — this is the "
            "12:08:33 clear, reproduced"
        )
        assert _emit(run, "KEY") == PRIOR_KEY, "a new episode was opened"
        assert run.page_state(PRIOR_KEY) == "still_open"
