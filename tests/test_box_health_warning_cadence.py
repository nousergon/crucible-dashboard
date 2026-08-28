"""A "no action urgent" finding must not page hourly.

WHY (alpha-engine-config-I7822, 2026-08-20). `box_health.sh` published the
`warning` tier with a 60-minute dedup window, the same as `critical`. The
standing `memory budget: BREACH` — a condition with an open decision on it
(#7804), explicitly labelled by its own prefix as *"budget/coverage finding (no
action urgent)"* — therefore produced **24 notifications in 24 hours** for one
unchanged condition, measured from `journalctl -u box-health.service`.

The tier below it, `info`, carried the same "(no action urgent)" label and was
already daily. Two tiers making the same promise at different cadences was the
inconsistency; the noisier one was wrong.

**Superseded in part the same day.** Lowering the window was necessary and not
sufficient: `info`/`warning` are "silent" only in the sense of
`disable_notification=True`, which suppresses the phone push and not the
message, so a daily info line still arrived in the chat every day. The `info`
tier no longer publishes to the channel at all — its findings go to the
console's fleet-check surface (`emit_box_health_hygiene.py`).

**Superseded again 2026-08-28 (alpha-engine-config-I9044).** `warning` kept its
channel publish on one stated premise: its delegated consumer, the Overseer
alert-drain, was not running. Re-measured, that premise is false — the drain's
four EventBridge *schedules* are still disabled and the drain has run daily
anyway, dispatched by `alpha-engine-freshness-monitor-cron` (ENABLED) through
the freshness-monitor Lambda (drain_ledger objects for 2026-08-24..08-28). So
`warning` is console-routed too. **The window survives the move and is still
asserted below**, because the channel is now the tier's FALLBACK: when the
console envelope cannot be written, this exact page is published with this
exact window. A window deleted as "no longer used" would be missing at the one
moment it matters.

**What makes lowering the cadence safe is the dedup KEY, not the window.**
`publish_problems` derives the key from the problem SET, so a warning
appearing, clearing, or changing its text produces a different key and pages
immediately whatever the window is. The window governs exactly one thing: how
often an UNCHANGED set is repeated. These tests pin that property, because it
is the only thing standing between "quieter" and "suppressed".
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH = REPO_ROOT / "infrastructure" / "box_health.sh"


def _publish_calls() -> dict[str, int]:
    """severity -> dedup window in minutes, from the tier publish lines."""
    out: dict[str, int] = {}
    for m in re.finditer(
        r"^publish_problems\s+(\w+)\s+(\d+)\s", BOX_HEALTH.read_text(), re.M
    ):
        out[m.group(1)] = int(m.group(2))
    return out


class TestTierCadence:
    def test_warning_repeats_monthly_not_daily(self):
        """60 -> 1440 -> 43200, and the second step for the same reason as the
        first: the tier is "silent" only in the sense of no phone push, so a
        daily unchanged warning is a daily VISIBLE message about a condition
        that already has a ruling on it (#7804). 43200 reuses the backstop
        interval publish_problems already applies to timer-job failures."""
        calls = _publish_calls()
        assert calls.get("warning") == 43200, (
            "the warning tier must repeat an unchanged set monthly — got "
            f"{calls.get('warning')}. 60 produced 24 pages in 24 hours; 1440 "
            "still produced a visible message most days for one standing "
            "memory-budget breach (alpha-engine-config-I7822, I7858)."
        )

    def test_info_has_no_cadence_because_it_has_no_channel_publish(self):
        """Superseded 2026-08-20 by the routing change, not relaxed.

        This assertion used to require `warning` and `info` to share a window,
        on the reasoning that two tiers carrying the same "(no action urgent)"
        label should not repeat at different rates. The premise was that both
        tiers were quiet in the channel. They were not: krepis.alerts'
        `SEVERITY_PUSH` is {error, critical} and everything else is published
        with `disable_notification=True`, which suppresses the phone push and
        NOT the message — so the info tier still landed in Brian's chat daily.

        `info` now has no `publish_problems` call at all; its findings go to the
        console's fleet-check surface. There is therefore no window to agree
        with, and asserting one would silently pass again the moment the
        channel publish came back.
        """
        calls = _publish_calls()
        assert "info" not in calls, (
            f"the info tier publishes to krepis.alerts again (window "
            f"{calls.get('info')}). A window is not the fix — the tier is "
            "visible in the channel at every severity below `error`."
        )
        src = BOX_HEALTH.read_text()
        assert "emit_hygiene_envelope" in src, (
            "the info tier has no channel publish AND no console emitter — "
            "that is suppression, not routing."
        )

    def test_the_warning_tier_is_console_routed_and_the_window_is_its_fallback(self):
        """The window is asserted above; this asserts where the page GOES.

        Reading a window off a `publish_problems` line says nothing about its
        DESTINATION, and destination is the whole of I9044. The behavioural
        proof — console when the console works, channel when it does not —
        lives in test_box_health_hygiene_console_routing.py, which runs the
        shipped shell; this pins the call site those tests would otherwise pass
        without.
        """
        call = re.search(
            r"^publish_problems\s+warning\s.*$", BOX_HEALTH.read_text(), re.M
        )
        assert call and call.group(0).rstrip().endswith("console"), (
            "the warning tier publishes straight to the operator's channel "
            f"again: {call.group(0) if call else 'no call site at all'}"
        )

    def test_critical_stays_hourly(self):
        """A degraded-now condition is worth repeating precisely because it is
        not standing. Quieting warnings must not quiet this."""
        calls = _publish_calls()
        assert calls.get("critical") == 60, (
            f"the critical tier must stay at 60 minutes, got {calls.get('critical')}"
        )


class TestSuppressionIsRepetitionOnly:
    def test_dedup_key_is_derived_from_the_problem_set(self):
        """The load-bearing property: a CHANGED set pages immediately whatever
        the window is. If the key ever stops depending on the problem text, the
        daily window becomes real suppression rather than deduplication."""
        src = BOX_HEALTH.read_text()
        i = src.index("publish_problems() {")
        body = src[i : src.index("\n}\n", i)]
        assert 'dkey="boxhealth-${severity}-$(printf' in body, (
            "the dedup key must be derived from the problem set; a static or "
            "severity-only key would make the daily window suppress new findings"
        )
        assert '${_problems[*]}' in body

    def test_severity_is_in_the_key(self):
        """A warning and a critical that happen to share text must not dedup
        against each other — the quieter one would swallow the louder."""
        src = BOX_HEALTH.read_text()
        i = src.index("publish_problems() {")
        body = src[i : src.index("\n}\n", i)]
        assert "boxhealth-${severity}-" in body


class TestTheFindingThatPrompted:
    def test_memory_budget_breach_is_console_only(self):
        """SUPERSEDED 2026-08-21: this used to assert the finding stayed a
        `warning`, on the reasoning that a quieter page must not become a
        vanished one.

        Brian ruled otherwise (alpha-engine-config-I7858) once the numbers were
        in: "if i'm 4x away from the wall then i certainly no longer want to be
        alerted of it." T1-8 is a HEADROOM bound; the wall is E3
        (`MemAvailable < 250 MB`), and MemAvailable measured 1128 MB while the
        breach stood.

        The original test's concern — "a tier nobody reads" — is answered
        rather than dismissed: the info tier does not publish to krepis.alerts,
        but `emit_hygiene_envelope` renders it on the console on every run with
        the finding's age. That is a tier someone reads on purpose instead of
        one that interrupts.
        """
        src = BOX_HEALTH.read_text()
        i = src.index("classify_problem_severity()")
        body = src[i : src.index("\n}\n", i)]
        assert '"memory budget: BREACH"*) echo info ;;' in body
        assert '"low memory: "*) echo critical ;;' in body, (
            "the actual out-of-memory condition must stay critical"
        )
