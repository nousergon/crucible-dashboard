"""The live systemd start-dependency graph, asserted on the box.

WHY THIS EXISTS
---------------
`Requires=x.service` in **x.timer's** `[Unit]` section is a START dependency of
the TIMER. `systemctl enable --now x.timer` in an installer therefore enqueues a
start job for `x.service` too — even when the timer is already active and no
calendar point has elapsed. On 2026-08-28 an unrelated `crucible-dashboard`
deploy thereby ran morning-signal's daily generator, its git pull, and a weekly
OSS bakeoff that spends real LLM tokens, at 03:00 UTC
(`alpha-engine-config-I9000`).

`crucible-dashboard-PR792` fixed the ten timers in this repo and guarded the
class with `test_no_install_path_starts_a_scheduled_workload.py`, which models
the same chain from SOURCE TEXT. Its own docstring names the two cases it
structurally cannot reach — a `systemctl restart "$unit"` whose unit name is a
shell variable, and units this repo does not install — and says the close is a
box-side assertion against live systemd. This file tests that assertion
(`alpha-engine-config-I9062`).

Measured live on `i-09b539c844515d549`, 2026-08-28: of the 35 enabled timers,
FOUR carrying the defect are not installed by this repo at all —
`daily-news.timer`, `ops-config-drift.timer`, `box-timer-health.timer` and
`systemd-unit-drift-check.timer`. No crucible-dashboard source-text test can
ever see them; `systemctl show` sees all four.

WHY THESE TESTS RUN THE SHELL INSTEAD OF READING IT
---------------------------------------------------
The repo's standing rule, from `box_health.sh::http_liveness_problems`: "a loop
proven only by reading it is a loop nobody has run." A static grep can assert
the words `X-InstallMayStart` appear in the script. It cannot catch a comment
matched as if it were a directive, a `case` whose word-boundary test lets
`morning-signal.service` match inside `morning-signal-pull.service`, or a
`systemctl` failure that yields a clean bill instead of a could-not-measure.
Every check below therefore extracts the SHIPPED function text out of
`box_health.sh` and evaluates it in a real bash, with `systemctl` faked at the
process boundary only.

WHY NONE OF THESE CAN PASS VACUOUSLY
------------------------------------
PR793 found two pre-existing static tests in this repo that were false-passing,
one of them by matching the file's own prose. Three properties keep that from
recurring here:

  * Every assertion is on the OUTPUT of executed shell, never on the presence of
    a string in a file. A deleted `echo` fails the test; a deleted comment does
    not. (The four wiring tests at the bottom are the deliberate exception, and
    each asserts an ORDERING between two anchors rather than a phrase.)
  * Each detecting test has a NEGATIVE twin asserting the same input produces
    NOTHING once one field changes — the exemption, or the dependency name. A
    check that fired unconditionally would pass the first and fail the second; a
    check that never fired would do the reverse. Neither can pass both.
  * `test_the_harness_would_notice_a_gutted_classifier` mutates the extracted
    source — deleting the finding `echo` — and asserts the harness then reports
    no finding. That is the guard against the harness testing nothing at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.box_health_helpers import (
    BOX_HEALTH,
    classify,
    function_source,
    global_assignment,
    run_lifecycle,
)

REPO_ROOT = Path(__file__).parent.parent

SCAN_FUNCTIONS = (
    "install_may_start_declared",
    "classify_install_start_dependency",
    "install_start_dependency_scan",
    "install_start_dependency_problems",
)
SCAN_GLOBALS = ("INSTALL_START_DEP_FINDINGS", "INSTALL_START_DEP_MEASURED")

FINDING_PREFIX = "notice: timer start-dependency:"


def _bash() -> str:
    b = shutil.which("bash")
    if not b:
        raise AssertionError("bash not available; cannot exercise the shipped functions")
    return b


def _run(
    body: str, tmp_path, fake_systemctl: str, *, mutate=None
) -> subprocess.CompletedProcess:
    """Run the SHIPPED scan functions with a faked `systemctl` on PATH.

    `fake_systemctl` is a bash script standing in for the real binary — the ONE
    process boundary that is replaced. Everything between it and the problem
    lines is the code that runs on the box.
    """
    binroot = tmp_path / "bin"
    binroot.mkdir(exist_ok=True)
    sc = binroot / "systemctl"
    sc.write_text("#!/bin/bash\n" + fake_systemctl)
    sc.chmod(0o755)

    sources = [function_source(f) for f in SCAN_FUNCTIONS]
    if mutate is not None:
        sources = [mutate(s) for s in sources]
    script = "\n".join(
        ["set -uo pipefail"]
        + [global_assignment(g) for g in SCAN_GLOBALS]
        + sources
        + [body]
    )
    child = dict(os.environ)
    child["PATH"] = f"{binroot}:{os.environ.get('PATH', '')}"
    return subprocess.run(
        [_bash(), "-c", script], capture_output=True, text=True, timeout=60, env=child
    )


# `systemctl` for a synthetic box. Four enabled timers:
#   good.timer     — no dependency on its own service (the healthy shape)
#   bad.timer      — Requires=bad.service, service NOT exempt          -> finding
#   exempt.timer   — Requires=exempt.service, service carries the PR792 key
#   foreign.timer  — BindsTo=foreign.service, described by no file in this repo
# plus a bare template and a disabled timer, both of which must be skipped.
_FAKE_SYSTEMCTL = r"""
case "$1" in
  list-unit-files)
    printf 'good.timer enabled enabled\nbad.timer enabled enabled\nexempt.timer enabled enabled\nforeign.timer enabled enabled\ntemplated@.timer enabled enabled\nparked.timer disabled disabled\n'
    exit 0 ;;
  is-enabled)
    # The real call is `systemctl is-enabled --quiet <unit>`, so the unit is the
    # LAST argument, not $2. Matching $2 would have let every timer through.
    case "${@: -1}" in parked.timer) exit 1 ;; *) exit 0 ;; esac ;;
  show)
    case "$2" in
      good.timer)    printf 'Unit=good.service\nRequires=sysinit.target -.mount\nRequisite=\nBindsTo=\nWants=\n' ;;
      bad.timer)     printf 'Unit=bad.service\nRequires=-.mount bad.service sysinit.target\nRequisite=\nBindsTo=\nWants=\n' ;;
      exempt.timer)  printf 'Unit=exempt.service\nRequires=exempt.service sysinit.target\nRequisite=\nBindsTo=\nWants=\n' ;;
      foreign.timer) printf 'Unit=foreign.service\nRequires=sysinit.target\nRequisite=\nBindsTo=foreign.service\nWants=\n' ;;
      *) exit 1 ;;
    esac
    exit 0 ;;
  cat)
    case "$2" in
      exempt.service)
        printf '# /etc/systemd/system/exempt.service\n[Unit]\nDescription=exempt\nX-InstallMayStart=yes\n[Service]\nType=oneshot\n' ;;
      bad.service|foreign.service|good.service)
        printf '# /etc/systemd/system/%s\n[Unit]\nDescription=x\n[Service]\nType=oneshot\n' "$2" ;;
      *) exit 1 ;;
    esac
    exit 0 ;;
esac
exit 0
"""


def _cat_fake(unit_text_path: Path) -> str:
    """A `systemctl` whose only job is to hand back one unit's text.

    Empty file => exit 1 with no output, i.e. the unreadable case.
    """
    return (
        'if [ "$1" = "cat" ]; then\n'
        f'  if [ -s "{unit_text_path}" ]; then cat "{unit_text_path}"; exit 0; fi\n'
        "  exit 1\n"
        "fi\nexit 0\n"
    )


# ── install_may_start_declared: yes / no / unknown ──────────────────────────


@pytest.mark.parametrize(
    "unit_text,expected",
    [
        pytest.param(
            "[Unit]\nDescription=x\nX-InstallMayStart=yes\n[Service]\nType=oneshot\n",
            "yes",
            id="assignment-under-Unit",
        ),
        pytest.param(
            "[Unit]\nDescription=x\nX-InstallMayStart = yes \n",
            "yes",
            id="whitespace-tolerated-as-systemd-does",
        ),
        # THE FALSE POSITIVE THAT WOULD OTHERWISE HAVE SHIPPED. PR792's exempt
        # units carry a multi-line `# X-InstallMayStart: <reason>` comment
        # immediately above the real directive, so a grep for the bare word
        # matches the rationale as readily as the key — the exact shape that put
        # a scheduled-identity scan on a YAML comment on 2026-08-27.
        pytest.param(
            "[Unit]\n# X-InstallMayStart: deliberately started by the installer\nDescription=x\n",
            "no",
            id="a-comment-explaining-the-key-is-not-the-key",
        ),
        pytest.param(
            "[Unit]\nDescription=x\n[Service]\nX-InstallMayStart=yes\n",
            "no",
            id="the-key-under-Service-is-not-an-exemption",
        ),
        pytest.param(
            "[Unit]\nX-InstallMayStart=no\n",
            "no",
            id="an-explicit-no-is-no",
        ),
        pytest.param(
            "# /etc/systemd/system/x.service\n[Unit]\nDescription=x\n"
            "# /etc/systemd/system/x.service.d/10-exempt.conf\n[Unit]\nX-InstallMayStart=yes\n",
            "yes",
            id="a-drop-in-declaration-counts-because-systemctl-cat-merges-it",
        ),
    ],
)
def test_the_exemption_is_read_from_the_live_unit_text(unit_text, expected, tmp_path):
    f = tmp_path / "unit.txt"
    f.write_text(unit_text)
    r = _run("install_may_start_declared some.service", tmp_path, _cat_fake(f))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


def test_a_systemctl_that_fails_yields_unknown_not_no(tmp_path):
    """"Could not read it" and "it is not exempt" are different facts.

    Folding the first into the second is how a detector reports a clean bill for
    a box it never measured.
    """
    f = tmp_path / "unit.txt"
    f.write_text("")
    r = _run("install_may_start_declared some.service", tmp_path, _cat_fake(f))
    assert r.stdout.strip() == "unknown"


# ── classify_install_start_dependency: the pure predicate ───────────────────


@pytest.mark.parametrize("directive", ["Requires", "Requisite", "BindsTo", "Wants"])
def test_every_start_pulling_directive_is_a_finding(directive, tmp_path):
    r = _run(
        "classify_install_start_dependency t.timer t.service "
        f'"-.mount t.service sysinit.target" {directive} no',
        tmp_path,
        "exit 0\n",
    )
    out = r.stdout.strip()
    assert out.startswith(FINDING_PREFIX), out
    assert "t.timer" in out and "t.service" in out and directive in out


def test_the_exemption_silences_the_same_input(tmp_path):
    """The negative twin of the test above: ONE field changes, nothing is said."""
    r = _run(
        'classify_install_start_dependency t.timer t.service "-.mount t.service" Requires yes',
        tmp_path,
        "exit 0\n",
    )
    assert r.stdout.strip() == ""


def test_systemds_own_implicit_edges_are_not_findings(tmp_path):
    """`sysinit.target` and `-.mount` sit on EVERY timer by default.

    A version of this check that flagged them would be tuned down and end up
    excluding the class it exists to catch — the failure mode `classify_identity`
    was born from (nous-ergon-ops-I155).
    """
    r = _run(
        'classify_install_start_dependency t.timer t.service "sysinit.target -.mount" Requires no',
        tmp_path,
        "exit 0\n",
    )
    assert r.stdout.strip() == ""


def test_a_dependency_on_a_different_service_is_not_a_finding(tmp_path):
    """Substring matching would let `morning-signal.service` match inside
    `morning-signal-pull.service` — a real pair on this box."""
    r = _run(
        "classify_install_start_dependency morning-signal.timer morning-signal.service "
        '"morning-signal-pull.service sysinit.target" Requires no',
        tmp_path,
        "exit 0\n",
    )
    assert r.stdout.strip() == ""


def test_an_unverifiable_exemption_is_could_not_measure_not_a_finding(tmp_path):
    r = _run(
        'classify_install_start_dependency t.timer t.service "t.service" Requires unknown',
        tmp_path,
        "exit 0\n",
    )
    out = r.stdout.strip()
    assert out.startswith("watchdog: "), out
    assert not out.startswith(FINDING_PREFIX)
    assert "t.service" in out


# ── install_start_dependency_scan: the live walk ────────────────────────────


def test_the_scan_reports_the_offenders_and_only_the_offenders(tmp_path):
    r = _run(
        "install_start_dependency_scan; install_start_dependency_problems",
        tmp_path,
        _FAKE_SYSTEMCTL,
    )
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    findings = [ln for ln in lines if ln.startswith(FINDING_PREFIX)]
    assert len(findings) == 2, lines
    assert any("bad.timer" in f and "Requires=bad.service" in f for f in findings)
    # A unit no file in this repo describes — the FOREIGN case the source-text
    # guard structurally cannot reach.
    assert any("foreign.timer" in f and "BindsTo=foreign.service" in f for f in findings)
    # ...and the healthy and exempt timers say nothing at all.
    assert not any("good.timer" in ln for ln in lines), lines
    assert not any("exempt.timer" in ln for ln in lines), lines


def test_a_parked_timer_and_a_bare_template_are_skipped(tmp_path):
    r = _run(
        "install_start_dependency_scan; install_start_dependency_problems",
        tmp_path,
        _FAKE_SYSTEMCTL,
    )
    assert "parked.timer" not in r.stdout
    assert "templated@.timer" not in r.stdout


def test_a_clean_box_is_measured_rather_than_silent(tmp_path):
    """Zero findings and a failed scan must not look the same."""
    clean = _FAKE_SYSTEMCTL.replace(
        "Requires=-.mount bad.service sysinit.target", "Requires=-.mount sysinit.target"
    ).replace("BindsTo=foreign.service", "BindsTo=")
    r = _run(
        'install_start_dependency_scan; echo "MEASURED=$INSTALL_START_DEP_MEASURED"; '
        "install_start_dependency_problems",
        tmp_path,
        clean,
    )
    assert "MEASURED=1" in r.stdout
    assert FINDING_PREFIX not in r.stdout


def test_an_unreadable_graph_is_could_not_measure_and_not_a_clean_bill(tmp_path):
    """principles.md §7 in one assertion.

    `systemctl list-unit-files` fails. The scan must (a) leave MEASURED at 0 so
    the count gauge is withheld and the series goes to missing-data rather than
    reporting a false zero, and (b) still SAY something on the problem stream. A
    run emitting neither would be indistinguishable from a healthy box.
    """
    r = _run(
        'install_start_dependency_scan; echo "MEASURED=$INSTALL_START_DEP_MEASURED"; '
        "install_start_dependency_problems",
        tmp_path,
        'if [ "$1" = "list-unit-files" ]; then exit 1; fi\nexit 0\n',
    )
    assert "MEASURED=0" in r.stdout
    said = [
        ln
        for ln in r.stdout.splitlines()
        if ln.startswith("watchdog: ") and "start-dependency" in ln
    ]
    assert said, r.stdout
    assert FINDING_PREFIX not in r.stdout


def test_a_timer_whose_properties_cannot_be_read_is_named_not_skipped(tmp_path):
    broken = _FAKE_SYSTEMCTL.replace(
        "      bad.timer)     printf", "      bad.timer)     exit 1 ;;\n      _unused)  printf"
    )
    r = _run(
        "install_start_dependency_scan; install_start_dependency_problems",
        tmp_path,
        broken,
    )
    assert any(
        ln.startswith("watchdog: ") and "bad.timer" in ln for ln in r.stdout.splitlines()
    ), r.stdout


def test_the_finding_set_is_identical_across_confirmation_samples(tmp_path):
    """`snapshot_problems` is sampled RETRY_ATTEMPTS times and keeps only lines
    present in EVERY sample. A finding that varied between replays would be
    filtered out by the very mechanism meant to suppress false positives — the
    defect that made the memory-pressure check unable to page."""
    r = _run(
        "install_start_dependency_scan; "
        "a=$(install_start_dependency_problems); b=$(install_start_dependency_problems); "
        '[ "$a" = "$b" ] && echo STABLE',
        tmp_path,
        _FAKE_SYSTEMCTL,
    )
    assert "STABLE" in r.stdout


def test_the_harness_would_notice_a_gutted_classifier(tmp_path):
    """Anti-vacuity: mutate the shipped source and prove the tests would fail.

    If deleting the finding `echo` still produced a finding, every assertion
    above would be measuring the harness rather than `box_health.sh`.
    """

    def gut(src: str) -> str:
        return "\n".join(
            ln
            for ln in src.splitlines()
            if not ln.strip().startswith('echo "notice: timer start-dependency:')
        )

    r = _run(
        "install_start_dependency_scan; install_start_dependency_problems",
        tmp_path,
        _FAKE_SYSTEMCTL,
        mutate=gut,
    )
    assert FINDING_PREFIX not in r.stdout, (
        "the mutant still reported a finding, so the passing tests above prove "
        "nothing about box_health.sh"
    )


def test_the_harness_would_notice_an_ignored_exemption(tmp_path):
    """The second mutant: delete the `yes` arm and the exempt timer must start
    reporting. Without this, a check that ignored `X-InstallMayStart` entirely
    would still pass every positive test above."""

    def gut(src: str) -> str:
        return "\n".join(
            ln for ln in src.splitlines() if ln.strip() != "yes) return 0 ;;"
        )

    r = _run(
        "install_start_dependency_scan; install_start_dependency_problems",
        tmp_path,
        _FAKE_SYSTEMCTL,
        mutate=gut,
    )
    assert "exempt.timer" in r.stdout, (
        "removing the exemption arm changed nothing, so the exemption is not "
        "what silences exempt.timer in the passing test above"
    )


# ── Tier: the console, never the channel ───────────────────────────────────


def test_the_finding_classifies_as_the_console_tier():
    """Brian's ruling, 2026-08-26: "i don't want to be paged with box health at
    all if there is no issue" — and `warning` is not quiet either, because
    krepis' `disable_notification` suppresses the phone push, not the message.
    A latent configuration footgun is not a live incident.
    """
    line = (
        "notice: timer start-dependency: daily-news.timer declares "
        "Requires=daily-news.service — an installer's `enable --now` starts it "
        "(alpha-engine-config-I9000)"
    )
    assert classify(line) == "info"


def test_the_could_not_measure_line_classifies_as_warning():
    """Detection blindness is reported, never paged — overseer-policy.md §3."""
    assert (
        classify(
            "watchdog: cannot read the live systemd start-dependency graph "
            "(install-start check did not run)"
        )
        == "warning"
    )


def test_the_finding_reaches_the_console_and_not_the_channel(tmp_path):
    """Executed end-to-end through the SHIPPED lifecycle, not inferred from the
    classifier. A line can classify as `info` and still reach krepis if the
    delivery path routes it there."""
    line = (
        "notice: timer start-dependency: daily-news.timer declares "
        "Requires=daily-news.service - an installer's enable --now starts it"
    )
    run = run_lifecycle(f'finalize_alert_lifecycle "{line}"', tmp_path)
    assert any("timer start-dependency" in ln for ln in run.console_lines), run.console_lines
    assert run.channel_pages == {}, run.channel_pages
    assert not any("timer start-dependency" in " ".join(c) for c in run.calls), run.calls


def test_the_console_row_carries_one_key_per_timer():
    """Several timers can carry the defect at once — four did on 2026-08-28. A
    shared key would collapse them onto one row, so fixing one would look like
    fixing all of them."""
    from infrastructure.emit_box_health_hygiene import finding_key

    a = finding_key(
        "notice: timer start-dependency: daily-news.timer declares Requires=daily-news.service"
    )
    b = finding_key(
        "notice: timer start-dependency: ops-config-drift.timer declares Requires=ops-config-drift.service"
    )
    assert a != b
    assert "daily-news.timer" in a and "ops-config-drift.timer" in b


# ── Wiring: the check has to actually be called ────────────────────────────


def test_the_scan_runs_once_per_run_and_before_the_samples():
    """Not inside `snapshot_problems`. That function is sampled RETRY_ATTEMPTS
    times, so scanning there would multiply ~140 `systemctl` invocations per tick
    for a graph that cannot change inside the window."""
    scan_at = BOX_HEALTH.index("\ninstall_start_dependency_scan\n")
    samples_at = BOX_HEALTH.index("confirmed=$(snapshot_problems)")
    assert scan_at < samples_at
    assert BOX_HEALTH.count("\ninstall_start_dependency_scan\n") == 1


def test_the_replay_is_wired_into_the_problem_stream():
    """A scan whose result nothing reads is a check that does not exist."""
    body = BOX_HEALTH[BOX_HEALTH.index("snapshot_problems() {"):]
    assert "    install_start_dependency_problems\n" in body


def test_the_gauge_is_withheld_when_the_graph_was_not_read():
    """`no data` is never rendered as green (principles.md §7). Publishing 0 on a
    failed scan is exactly that rendering."""
    i = BOX_HEALTH.index("timers_with_install_start_dependency")
    guard = BOX_HEALTH.rindex('if [ "$INSTALL_START_DEP_MEASURED" -eq 1 ]; then', 0, i)
    assert guard < i


def test_the_gauge_is_published_before_the_all_healthy_exit():
    """The all-healthy path `exit 0`s long before the problem-derived gauges near
    the bottom of the script. A gauge that only exists on unhealthy runs cannot
    be told from a dead emitter — which is precisely what this check is about, so
    it may not have that shape itself."""
    gauge_at = BOX_HEALTH.index("timers_with_install_start_dependency")
    first_exit_at = BOX_HEALTH.index("confirmed=$(snapshot_problems)")
    assert gauge_at < first_exit_at


def test_the_three_exempt_service_names_are_not_restated_in_the_script():
    """The `X-InstallMayStart=yes` declaration in the unit is the single source
    of truth PR792 established. A second copy here is a second thing to keep in
    sync, and it would be the one that won the day they disagreed."""
    scan = "\n".join(function_source(f) for f in SCAN_FUNCTIONS)
    for name in (
        "emit-oom-metric.service",
        "emit-service-memory.service",
        "box-state-backup.service",
    ):
        assert name not in scan, f"{name} is restated in box_health.sh"
