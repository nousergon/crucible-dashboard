"""Run box_health.sh's own severity classifier, as shipped.

WHY THIS EXECUTES BASH INSTEAD OF RE-IMPLEMENTING THE CASE STATEMENT
--------------------------------------------------------------------
A Python transcription of the classifier would be a second implementation, and a
test of a second implementation proves only that the two agree at the moment
someone wrote them. The fleet has already paid for that lesson twice on this very
script: a PSI parser that could never fire passed its fixtures, and a `|| echo
000` appended to a self-defaulting curl passed every fixture test of the
predicate while silently disarming the shipped detector.

So this extracts the literal text of `classify_problem_severity` out of
`box_health.sh` and evaluates it in a real bash. What the test exercises is the
bytes that run on the box.

Extraction is deliberately anchored and asserted rather than best-effort: if the
function is renamed, moved, or its closing brace de-indented, `classifier_source`
raises instead of returning something that silently classifies nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH_PATH = REPO_ROOT / "infrastructure" / "box_health.sh"
BOX_HEALTH = BOX_HEALTH_PATH.read_text()

_FUNC_RE = re.compile(
    r"^classify_problem_severity\(\)\s*\{.*?^\}", re.MULTILINE | re.DOTALL
)


def classifier_source() -> str:
    """The literal shell text of the classifier, or raise."""
    m = _FUNC_RE.search(BOX_HEALTH)
    if not m:
        raise AssertionError(
            "classify_problem_severity() not found in box_health.sh. It is the "
            "single point where a problem line becomes a page or a silence; if "
            "it was renamed or restructured, these tests must be updated "
            "deliberately, not skipped."
        )
    return m.group(0)


def classify(problem_line: str) -> str:
    """Classify one problem line by running the shipped bash function."""
    bash = shutil.which("bash")
    if not bash:
        raise AssertionError("bash not available; cannot exercise the shipped function")
    script = classifier_source() + '\nclassify_problem_severity "$1"\n'
    r = subprocess.run(
        [bash, "-c", script, "_", problem_line],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise AssertionError(
            f"classify_problem_severity exited {r.returncode} for {problem_line!r}: "
            f"{r.stderr.strip()}"
        )
    return r.stdout.strip()


# Anchored on the block's START and on the NEXT statement after it, deliberately
# not on its last line. Anchoring on the trailing-newline strip would make
# deleting that strip look like "block not found" rather than like the phantom
# empty bullet it actually causes — a guard that fails for the wrong reason is a
# guard that stops meaning anything the day the message changes.
_PARTITION_RE = re.compile(
    r'^criticals=""; warnings=""; notices=""$.*?(?=^publish_verdict )',
    re.MULTILINE | re.DOTALL,
)


def partition_source() -> str:
    """The literal shell text of the tier partition block, or raise."""
    m = _PARTITION_RE.search(BOX_HEALTH)
    if not m:
        raise AssertionError(
            "the tier partition block was not found in box_health.sh. It is "
            "what turns classified lines into the three published payloads; if "
            "it moved, these tests must be updated deliberately."
        )
    return m.group(0)


def partition(confirmed: list[str]) -> dict[str, list[str]]:
    """Run the SHIPPED partition on a confirmed problem set.

    Each tier is then fed through the same `mapfile -t ... <<< "$lines"` that
    publish_problems uses, and the resulting elements are what this returns —
    so a trailing-newline defect that produces a phantom empty bullet shows up
    here as an empty element, exactly as it would in a real alert.
    """
    bash = shutil.which("bash")
    if not bash:
        raise AssertionError("bash not available; cannot exercise the shipped function")
    script = "\n".join([
        classifier_source(),
        'confirmed="$1"',
        partition_source(),
        # Mirror publish_problems' own mapfile, then emit one marker per element.
        'for tier in criticals warnings notices; do',
        '  eval "payload=\\$$tier"',
        '  [ -z "$payload" ] && continue',
        '  mapfile -t _p <<< "$payload"',
        '  for e in "${_p[@]}"; do printf "%s\\t%s\\n" "$tier" "$e"; done',
        'done',
    ])
    r = subprocess.run(
        [bash, "-c", script, "_", "\n".join(confirmed)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise AssertionError(f"partition block exited {r.returncode}: {r.stderr.strip()}")
    out: dict[str, list[str]] = {"criticals": [], "warnings": [], "notices": []}
    for row in r.stdout.split("\n"):
        if "\t" not in row:
            continue
        tier, _, entry = row.partition("\t")
        out[tier].append(entry)
    return out


def emitted_problem_lines() -> list[str]:
    """Every problem string box_health.sh can put into the confirmed set.

    Problems are the script's STDOUT lines; diagnostics go to stderr and are not
    problems. So this collects `echo "..."` with no `>&2` redirect, from inside
    the problem-producing functions, and drops the shell-variable interpolation
    so the result is a stable prefix the classifier can be asked about.

    A line this misses is a line the totality test cannot protect, which is why
    the test also asserts the harvested count against a floor.
    """
    lines: list[str] = []
    for raw in BOX_HEALTH.splitlines():
        stripped = raw.strip()
        if not stripped.startswith('echo "'):
            continue
        if ">&2" in stripped:
            continue
        m = re.match(r'echo "([^"]*)"', stripped)
        if not m:
            continue
        text = m.group(1)
        # Only lines that begin with literal text can be classified by prefix; a
        # line starting with an interpolation has no stable prefix and would be
        # a design problem in its own right.
        if text.startswith("$"):
            continue
        lines.append(text)
    return lines
