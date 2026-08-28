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

import base64
import os
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


# ── The alert-lifecycle harness: extract the routing, then RUN it ───────────
#
# WHY THIS EXECUTES THE SHIPPED BASH, like `classify` above and for the same
# reason. The routing decision this harness exists to prove -- does a clear go
# to the operator's channel or to the console -- is a property of a `case`
# statement, two array accumulators and the ORDER of four calls inside
# `finalize_alert_lifecycle`. A static grep over box_health.sh can assert that
# the words are present; it cannot catch a clear queued to the console and never
# rendered, a fallback that runs before the state file it reads is rewritten, or
# a loop whose second iteration never happens because a child ate stdin. This
# repo has already paid for that twice on this file (`http_liveness_problems`:
# "a loop proven only by reading it is a loop nobody has run").
#
# So the real functions are lifted out and run against fakes at the PROCESS
# boundary only -- the krepis CLI, the console emitter's interpreter, and `aws`.
# Everything between the state file and those three boundaries is the shipped
# code.

LIFECYCLE_FUNCTIONS = (
    "krepis_supports_clear",
    "krepis_clear_supported",
    "krepis_push_set_load",
    "clear_destination",
    "krepis_supports_publish_lifecycle",
    "krepis_publish_lifecycle_args",
    "alerted_state_prior",
    "alerted_state_lifecycle",
    "alerted_state_write",
    "timer_failure_dedup_key",
    "emit_hygiene_envelope",
    "publish_page",
    "publish_problems",
    "publish_clears",
    "publish_channel_clear",
    "console_route_fallback",
    "publish_unpublished_clears",
    "finalize_alert_lifecycle",
)

LIFECYCLE_GLOBALS = (
    "ALERTED_NOW",
    "UNPUBLISHED_CLEARS",
    "CONSOLE_ROUTED_LINES",
    "CONSOLE_CLEAR_KEYS",
    "CONSOLE_CLEAR_MSGS",
    "DEFERRED_PAGE_SEV",
    "DEFERRED_PAGE_WIN",
    "DEFERRED_PAGE_DKEY",
    "DEFERRED_PAGE_MSG",
    "DEFERRED_PAGE_COUNT",
    "KREPIS_CLEAR",
    "KREPIS_PUSH_SET",
    "KREPIS_PUBLISH_LIFECYCLE",
)

US = "\x1f"  # argv separator in the fake CLI's log


def function_source(name: str) -> str:
    """The literal shell text of one top-level function, or raise.

    Anchored on `^name() {` and the first `^}`; a rename or a de-indented
    closing brace fails loudly rather than yielding a harness that silently
    tests nothing.
    """
    m = re.search(
        rf"^{re.escape(name)}\(\)\s*\{{.*?^\}}", BOX_HEALTH, re.MULTILINE | re.DOTALL
    )
    if not m:
        raise AssertionError(
            f"{name}() not found in box_health.sh. The alert-lifecycle harness "
            "extracts it by name; if it was renamed or restructured these tests "
            "must be updated deliberately, not skipped."
        )
    return m.group(0)


def global_assignment(name: str) -> str:
    """The literal shell text of one top-level global's initialisation."""
    m = re.search(rf"^{re.escape(name)}=.*$", BOX_HEALTH, re.MULTILINE)
    if not m:
        raise AssertionError(
            f"{name} is no longer initialised at the top level of box_health.sh. "
            "`set -u` turns an uninitialised accumulator into a dead watchdog, "
            "which is why each one is asserted here."
        )
    return m.group(0)


class LifecycleRun:
    """What one harness run did, at the three process boundaries."""

    def __init__(self, proc, calls, console_payloads, metrics):
        self.proc = proc
        self.calls = calls                        # list[list[str]] krepis CLI argv
        self.console_payloads = console_payloads  # list[str] emitter stdin
        self.metrics = metrics                    # list[str] `aws` argv

    def _sub(self, verb):
        return [
            c for c in self.calls
            if len(c) >= 3 and c[1] == "krepis.alerts" and c[2] == verb
        ]

    @property
    def channel_clears(self):
        """identity-key -> message, for every terminator sent to the channel."""
        out = {}
        for c in self._sub("clear"):
            out[c[c.index("--identity-key") + 1]] = c[c.index("--message") + 1]
        return out

    @property
    def channel_pages(self):
        """dedup-key -> severity, for every page sent to the channel."""
        out = {}
        for c in self._sub("publish"):
            out[c[c.index("--dedup-key") + 1]] = c[c.index("--severity") + 1]
        return out

    def page_state(self, dedup_key):
        for c in self._sub("publish"):
            if c[c.index("--dedup-key") + 1] == dedup_key and "--state" in c:
                return c[c.index("--state") + 1]
        return None

    @property
    def console_lines(self):
        lines = []
        for payload in self.console_payloads:
            lines += [ln for ln in payload.splitlines() if ln.strip()]
        return lines

    @property
    def clears_unpublished(self):
        """The value published to the health_clears_unpublished series."""
        for m in self.metrics:
            if "health_clears_unpublished" in m:
                return int(re.search(r"Value=(\d+)", m).group(1))
        raise AssertionError(
            "health_clears_unpublished was not published at all. A gauge that "
            "only appears when it is non-zero cannot be told from a dead emitter."
        )


_FAKE_ALERT_PY = r"""#!/bin/bash
# Stands in for the krepis venv interpreter at the PROCESS boundary only.
if [ "$1" = "-c" ]; then
    case "$2" in
        *SEVERITY_PHONE_PUSH*) echo "${FAKE_PUSH_SET-critical error}"; exit 0 ;;
        *publish_clear*)       exit "${FAKE_CLEAR_SUPPORTED-0}" ;;
        *signature*)           exit 0 ;;
    esac
    exit 0
fi
# base64 per argument: a clear's message is multi-line by construction, and
# one call must stay one log line.
{ for a in "$@"; do printf '%s' "$a" | base64 | tr -d '\n'; printf '\037'; done; printf '\n'; } >> "$FAKE_CLI_LOG"
if [ "${3-}" = "clear" ]; then exit "${FAKE_CLEAR_RC-0}"; fi
if [ "${3-}" = "publish" ]; then exit "${FAKE_PUBLISH_RC-0}"; fi
exit 0
"""

_FAKE_VENV_PY = r"""#!/bin/bash
# Stands in for the console emitter's interpreter. Records exactly what
# box_health.sh piped to it, then succeeds or fails on demand.
cat >> "$FAKE_CONSOLE_LOG"
printf '\037END\n' >> "$FAKE_CONSOLE_LOG"
exit "${FAKE_CONSOLE_RC-0}"
"""

_FAKE_AWS = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_METRIC_LOG"
exit 0
"""


def run_lifecycle(body: str, tmp_path, overrides: dict | None = None) -> LifecycleRun:
    """Run the SHIPPED alert-lifecycle functions with `body` as the scenario."""
    bash = shutil.which("bash")
    if not bash:
        raise AssertionError(
            "bash not available; cannot exercise the shipped functions"
        )

    binroot = tmp_path / "bin"
    binroot.mkdir(exist_ok=True)
    for name, text in (
        ("alert_py", _FAKE_ALERT_PY),
        ("venv_py", _FAKE_VENV_PY),
        ("aws", _FAKE_AWS),
    ):
        f = binroot / name
        f.write_text(text)
        f.chmod(0o755)

    cli_log = tmp_path / "cli.log"
    console_log = tmp_path / "console.log"
    metric_log = tmp_path / "metric.log"
    for f in (cli_log, console_log, metric_log):
        f.write_text("")
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    emitter_stub = tmp_path / "emit_box_health_hygiene.py"
    emitter_stub.write_text("# readable sibling; the fake interpreter is what runs\n")

    preamble = "\n".join([
        "set -uo pipefail",
        f'ALERT_PY="{binroot / "alert_py"}"',
        f'VENV_PY="{binroot / "venv_py"}"',
        f'HYGIENE_EMITTER="{emitter_stub}"',
        'INSTANCE_ID="i-harness"',
        f'THROTTLE_STATE_DIR="{state_dir}"',
        f'ALERTED_STATE="{state_dir / "alerted"}"',
        "UNALERTED_CRITICALS=0",
    ])
    script = "\n".join(
        [preamble]
        + [global_assignment(g) for g in LIFECYCLE_GLOBALS]
        + [function_source(f) for f in LIFECYCLE_FUNCTIONS]
        + [body]
    )

    child = dict(os.environ)
    child["PATH"] = f"{binroot}:{os.environ.get('PATH', '')}"
    child["FAKE_CLI_LOG"] = str(cli_log)
    child["FAKE_CONSOLE_LOG"] = str(console_log)
    child["FAKE_METRIC_LOG"] = str(metric_log)
    child.update(overrides or {})
    proc = subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, timeout=60, env=child
    )

    calls = []
    for row in cli_log.read_text().splitlines():
        if not row:
            continue
        calls.append(
            [base64.b64decode(a).decode() for a in row.split(US)[:-1] if a != ""]
        )
    payloads = [p for p in console_log.read_text().split(f"{US}END\n") if p != ""]
    return LifecycleRun(proc, calls, payloads, metric_log.read_text().splitlines())
