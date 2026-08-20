"""A "no action urgent" finding must not page hourly.

WHY (alpha-engine-config-I7816, 2026-08-20). `box_health.sh` published the
`warning` tier with a 60-minute dedup window, the same as `critical`. The
standing `memory budget: BREACH` — a condition with an open decision on it
(#7804), explicitly labelled by its own prefix as *"budget/coverage finding (no
action urgent)"* — therefore produced **24 notifications in 24 hours** for one
unchanged condition, measured from `journalctl -u box-health.service`.

The tier below it, `info`, carries the same "(no action urgent)" label and was
already daily. Two tiers making the same promise at different cadences is the
inconsistency; the noisier one was wrong.

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
    def test_warning_is_daily_not_hourly(self):
        calls = _publish_calls()
        assert calls.get("warning") == 1440, (
            "the warning tier must repeat an unchanged set daily, not hourly — "
            f"got {calls.get('warning')}. 60 produced 24 pages in 24 hours for "
            "one standing memory-budget breach (alpha-engine-config-I7816)."
        )

    def test_warning_and_info_agree(self):
        """Both prefixes end in '(no action urgent)'. Two tiers making the same
        promise at different cadences is what produced the noise."""
        calls = _publish_calls()
        assert calls.get("warning") == calls.get("info"), (
            "warning and info both label themselves '(no action urgent)' and "
            f"must repeat at the same cadence — warning={calls.get('warning')} "
            f"info={calls.get('info')}"
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
    def test_memory_budget_breach_is_still_a_warning(self):
        """This change makes the page quieter. It must not also make it
        disappear, or reclassify the finding to a tier nobody reads."""
        src = BOX_HEALTH.read_text()
        i = src.index("classify_problem_severity()")
        body = src[i : src.index("\n}\n", i)]
        assert '"memory budget: BREACH"*) echo warning ;;' in body
