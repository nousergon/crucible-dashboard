"""No install or deploy path may start a scheduled workload service.

WHY
---
2026-08-28 03:00:42 UTC, `crucible-dashboard-PR788` merged. Its deploy ran
`install-morning-signal.sh`, and within four seconds `morning-signal-pull.service`
and `morning-signal-bakeoff.service` started, followed by `morning-signal.service`
once the `daily-news.service` it orders after had finished. None were due —
`morning-signal-pull.timer` next elapsed 11:55 UTC, `morning-signal.timer` 11:00,
`morning-signal-bakeoff.timer` the following Wednesday. All three failed, box-health
paged, and the weekly OSS bakeoff spent real LLM tokens on a Friday morning
(alpha-engine-config-I9000).

THE MECHANISM, measured, not assumed
------------------------------------
`Requires=` in a TIMER's `[Unit]` section is a START dependency **of the timer**,
not a declaration of what the timer triggers. `systemctl enable --now <x>.timer`
in an installer therefore enqueues a start job for `<x>.service` as well — and it
does so even when the timer is already active and no calendar point has elapsed,
because the transaction still carries the dependency jobs.

`Persistent=true` catch-up was the standing hypothesis and it is WRONG. The stamp
files disprove it: at the time of the incident
`/var/lib/systemd/timers/stamp-morning-signal-bakeoff.timer` and the unit's own
`LastTriggerUSec` both still read `Wed 2026-08-26 12:00:01` — no elapse was
recorded at 03:00 on 08-28 — while the journal shows the service starting 0.5s
after the installer's `daemon-reload`. A `Persistent=` replay writes the stamp;
a dependency-pulled start does not.

WHAT THIS TEST PINS
-------------------
The causal chain has two links, and breaking either one is a valid fix:

    installer says `enable --now <timer>` / `start <unit>`
        -> the unit's [Unit] dependencies pull in <service>
            -> <service> is a scheduled workload and runs off-schedule

So this is deliberately NOT a grep for `--now`. It models the chain: it reads the
start-shaped `systemctl` invocations out of every `install-*.sh` and
`deploy-on-merge.sh`, expands each started unit through the `Requires=` /
`Requisite=` / `BindsTo=` / `Wants=` edges declared in this repo's own unit files
and drop-ins, and fails if the resulting start set contains a scheduled workload.
Restoring `Requires=<x>.service` to a timer fails it again; so would adding
`systemctl start morning-signal.service` to an installer while the timers stay
clean. Removing either link passes, which is correct — either alone breaks the
chain.

A "scheduled workload" is derived, not listed: a `Type=oneshot` service that some
timer in this repo names as its `Unit=` (or triggers by the same-basename default).
That is exactly the class whose whole contract is "runs when the clock says so".

THE ESCAPE HATCH LIVES IN THE UNIT, NOT HERE
--------------------------------------------
Three services in this repo are started by an install path on purpose. Each
declares `X-InstallMayStart=yes` under its own `[Unit]`, with the reason written
beside it — the same shape as `X-DeadManStaleness=` (see
`test_every_installed_timer_has_a_deadman_row.py`), so the justification sits next
to the thing it justifies and travels with a unit that moves repos. `X-` keys are
ignored by systemd, so this costs nothing on the box. A declaration that no script
actually exercises is also failed, so the set cannot rot into a stale allowlist.

SCOPE, stated because it bounds the guarantee
---------------------------------------------
Source-text analysis, matching this repo's other installer guards: the installers
run as root on an EC2 box against `/home/ec2-user` paths, and executing them in CI
is not meaningful. What is pinned is the CONTRACT between the scripts and the unit
files in this repo.

Two gaps this cannot see, named rather than left implicit:
  * a `systemctl restart "$unit"` whose unit name is a shell variable
    (`install-resource-limits.sh` renders its set from `budget.yaml`) — tokens
    containing `$` are skipped;
  * dependency edges declared by units this repo does not own (`daily-news.*` is
    installed by another repo), so a chain that leaves and re-enters this repo's
    unit set is invisible here.
To close them properly the check has to run against the live systemd
(`systemctl show <timer> -p Requires`) on the box — which `box_health.sh` could
carry as a box-side assertion. Not built here.

WHERE THE PARSER LIVES
----------------------
`nousergon_lib.systemd_install_guard`, since 2026-08-28. This test grew twins in
`nousergon-data` and `nous-ergon-ops` the same day, and the twins had already
fixed shell-noise defects this copy still carried (`>/dev/null.service` and
`2>.service` reported as unit names) — one behaviour, three implementations,
two of them ahead of the third. `shared-code-policy`'s second-adoption trigger
had fired at the second copy; the lift is `alpha-engine-config-I9099`.

What stays HERE is what is repo-specific and must not become a library
allowlist: the paths, the presence assertions above, this narrative, and the
failure message that names this repo's fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nousergon_lib.systemd_install_guard import (
    load_units,
    may_start,
    scheduled_workloads,
    start_closure,
    started_units,
    violations,
)

INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
SYSTEMD = INFRA / "systemd"

UNITS = load_units(SYSTEMD)


def _install_paths() -> list[Path]:
    paths = sorted(INFRA.glob("install-*.sh"))
    paths.append(INFRA / "deploy-on-merge.sh")
    return [p for p in paths if p.exists()]


def test_the_repo_still_has_scheduled_workloads_to_protect():
    """Guard against the guard silently covering nothing."""
    workloads = scheduled_workloads(UNITS)
    assert "morning-signal-bakeoff.service" in workloads, workloads
    assert "morning-signal.service" in workloads, workloads
    assert len(workloads) >= 8, workloads


def test_install_paths_are_parsed_at_all():
    """Guard against a regex that matches nothing reporting a clean sweep."""
    paths = _install_paths()
    assert len(paths) >= 10, paths
    assert any(started_units(p) for p in paths)
    installer = INFRA / "install-morning-signal.sh"
    assert "morning-signal.timer" in started_units(installer)


def test_no_reported_unit_name_is_shell_noise():
    """Every name the parser reports must look like a unit, in every installer.

    This is the assertion the pre-lift copy in THIS repo could not make: it
    reported `>/dev/null.service` and `2>.service` out of
    `systemctl enable --now x.timer >/dev/null 2>&1 || true`. Verdict-neutral,
    and exactly the garbage that gets a guard argued with the day it fires.
    """
    noise = {
        unit
        for script in _install_paths()
        for unit in started_units(script)
        if not unit.endswith((".timer", ".service")) or set("<>&|#") & set(unit)
    }
    assert not noise, sorted(noise)


@pytest.mark.parametrize("script", _install_paths(), ids=lambda p: p.name)
def test_no_install_path_starts_a_scheduled_workload(script: Path):
    offenders = violations(script, UNITS)
    assert not offenders, (
        f"{script.name} activates a scheduled workload off-schedule: "
        + "; ".join(
            f"`systemctl start {unit}` pulls in {sorted(pulled)}"
            for unit, pulled in sorted(offenders.items())
        )
        + ". A deploy must never run a timer-driven job. Either drop the start "
        "(`enable` without `--now` still arms the timer) or drop the [Unit] "
        "dependency edge that pulls the service in — a timer binds its service "
        "with `Unit=`, never with `Requires=`. If the start is deliberate and "
        "free, declare `X-InstallMayStart=yes` in that service's [Unit] with "
        "the reason beside it (alpha-engine-config-I9000)."
    )


def test_every_install_may_start_declaration_is_still_exercised():
    """A declaration nobody uses is a stale allowlist entry, not a rationale."""
    declared = {u for u in UNITS if may_start(u, UNITS)}
    assert declared, "the escape hatch has no users — did the key get renamed?"
    started: set[str] = set()
    for script in _install_paths():
        for unit in started_units(script):
            started |= start_closure(unit, UNITS)
    unused = declared - started
    assert not unused, (
        f"X-InstallMayStart=yes declared on {sorted(unused)} but no install or "
        "deploy path starts it. Remove the declaration."
    )
