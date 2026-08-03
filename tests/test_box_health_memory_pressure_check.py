"""The per-service memory-pressure check must be able to fire.

WHY THIS EXISTS
---------------
box_health.sh's cgroup memory-pressure check (alpha-engine-config-I4512) read
PSI with `awk '/^some avg10/{val=$2+0; ...}'`. PSI does not emit columns, it
emits KEY=VALUE:

    some avg10=55.27 avg60=53.82 avg300=51.32 total=525043914

so `$2` is the STRING "avg10=55.27" and `$2+0` is 0. The predicate `val>10`
was false for every input, including the ones the check exists to catch. It
shipped, was reviewed, and ran every 10 minutes for weeks without ever being
able to emit a line.

Measured consequence, 2026-08-03: vires.service was pinned at MemoryHigh=112M
against a ~316 MB working set and sat at `some avg10=55.27 / full avg300=49.93`
for ~18 minutes. Every HTTP request hung; the app was unusable. Four
consecutive box-health ticks ran during that window and this check said
nothing, because it could not say anything. The port check could not see it
either (the socket stays bound while the server is wedged), so the box reported
healthy throughout and the outage was found by a human using the app.

These tests execute the ACTUAL awk expression from box_health.sh against real
PSI text. They fail against the pre-fix expression -- which is the only kind of
evidence that a guard works, per the standing rule that a check must be shown
to fail against unfixed input.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH_PATH = REPO_ROOT / "infrastructure" / "box_health.sh"
BOX_HEALTH = BOX_HEALTH_PATH.read_text()

# Real reading taken from /sys/fs/cgroup/system.slice/vires.service/memory.pressure
# on the dashboard box at 15:33 UTC on 2026-08-03, mid-incident.
STALLED_PSI = (
    "some avg10=55.27 avg60=53.82 avg300=51.32 total=525043914\n"
    "full avg10=53.83 avg60=52.38 avg300=49.93 total=510829287\n"
)
# A healthy service on the same box at the same moment.
QUIET_PSI = (
    "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
)


def _pressure_awk_program() -> str:
    """The awk program box_health.sh actually runs, lifted from its source.

    Lifted rather than duplicated: a copy in the test would keep passing after
    someone edited the script, which is the failure mode this whole file is
    about.
    """
    m = re.search(r"pressure=\$\(awk '([^']+)'", BOX_HEALTH)
    assert m, "could not find the memory-pressure awk expression in box_health.sh"
    return m.group(1)


def _run_pressure_awk(psi_text: str, tmp_path: Path) -> str:
    f = tmp_path / "memory.pressure"
    f.write_text(psi_text)
    return subprocess.run(
        ["awk", _pressure_awk_program(), str(f)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_fires_on_a_service_stalled_on_reclaim(tmp_path):
    """55% stall must produce output. The pre-fix expression produced none."""
    out = _run_pressure_awk(STALLED_PSI, tmp_path)
    assert out, (
        "the memory-pressure check emitted nothing for a cgroup stalled 55% of "
        "the time on reclaim -- it cannot detect the condition it exists for"
    )
    assert float(out) > 10


def test_silent_on_a_healthy_service(tmp_path):
    """The complement: a quiet cgroup must not page."""
    assert _run_pressure_awk(QUIET_PSI, tmp_path) == ""


def test_threshold_is_exclusive_and_reads_the_real_field(tmp_path):
    """10.5% fires, 9.5% does not -- proves the VALUE is parsed, not the key."""
    just_over = "some avg10=10.50 avg60=0.00 avg300=0.00 total=1\n"
    just_under = "some avg10=9.50 avg60=99.00 avg300=99.00 total=1\n"
    assert _run_pressure_awk(just_over, tmp_path) != ""
    # avg60/avg300 are deliberately high here: a parser reading the wrong field
    # would fire on this line, and that is a different bug wearing this fix.
    assert _run_pressure_awk(just_under, tmp_path) == ""


def test_the_emitted_problem_line_carries_no_moving_number():
    """LOAD-BEARING: snapshot_problems confirms by exact line intersection.

    Problem lines are sampled RETRY_ATTEMPTS times and only lines present in
    EVERY sample are alerted. A PSI average moves between samples, so a line
    interpolating it can never confirm -- the check would still be silent with
    a correct parser. This is the same rule
    test_box_health_budget_wiring.test_budget_problem_strings_are_static pins
    for the `memory budget:` prefixes; it did not cover this one.
    """
    emitted = re.findall(r'echo "(memory pressure:[^"]*)"', BOX_HEALTH)
    assert emitted, "expected a `memory pressure: ...` problem line"
    for line in emitted:
        assert "${pressure}" not in line, (
            f"problem line interpolates the live PSI value and can never "
            f"confirm across samples: {line}"
        )
        assert not re.search(r"\d+\s*(MB|MiB|GB|%|x)", line), line


def test_pressure_detail_still_reaches_the_journal():
    """Making the line static must not discard the number entirely -- the
    diagnosis needs it. It moves to stderr, like the budget check's detail."""
    assert "memory pressure detail" in BOX_HEALTH
