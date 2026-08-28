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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
SYSTEMD = INFRA / "systemd"

# systemctl verbs that cause a unit to be ACTIVATED. `enable` alone and
# `daemon-reload` do not, which is why they are absent.
_START_RE = re.compile(
    r"systemctl\s+(?P<flags>(?:--\S+\s+)*)"
    r"(?P<verb>start|restart|enable|reload-or-restart|try-restart)\s+"
    r"(?P<rest>[^\n;|&)]*)"
)
_DEP_KEYS = ("Requires", "Requisite", "BindsTo", "Wants")


def _sections(text: str) -> dict[str, list[str]]:
    """Split a unit file into {section: [lines]}, ignoring comments."""
    out: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out.setdefault(current, [])
            continue
        out.setdefault(current, []).append(line)
    return out


def _directive(lines: list[str], key: str) -> list[str]:
    vals: list[str] = []
    for line in lines:
        k, _, v = line.partition("=")
        if k.strip() == key:
            vals.extend(v.split())
    return vals


def _load_units() -> dict[str, dict[str, list[str]]]:
    """Every unit this repo installs, with its drop-ins merged in."""
    units: dict[str, dict[str, list[str]]] = {}
    for path in sorted(SYSTEMD.glob("*")):
        if path.is_dir() or path.suffix not in (".service", ".timer"):
            continue
        units[path.name] = _sections(path.read_text())
    for dropin_dir in sorted(SYSTEMD.glob("*.service.d")):
        target = units.setdefault(dropin_dir.name[: -len(".d")], {})
        for conf in sorted(dropin_dir.glob("*.conf")):
            for section, lines in _sections(conf.read_text()).items():
                target.setdefault(section, []).extend(lines)
    return units


UNITS = _load_units()


def _triggered_service(timer: str, sections: dict[str, list[str]]) -> str:
    explicit = _directive(sections.get("Timer", []), "Unit")
    if explicit:
        return explicit[-1]
    return timer[: -len(".timer")] + ".service"


def _scheduled_workloads() -> set[str]:
    """Type=oneshot services that a timer in this repo exists to trigger."""
    out = set()
    for name, sections in UNITS.items():
        if not name.endswith(".timer"):
            continue
        target = _triggered_service(name, sections)
        target_sections = UNITS.get(target)
        if target_sections is None:
            continue
        if "oneshot" in _directive(target_sections.get("Service", []), "Type"):
            out.add(target)
    return out


def _normalise(token: str) -> str | None:
    token = token.strip().strip("\"'")
    if not token or token.startswith("-") or "$" in token or "*" in token:
        return None
    if "." not in token.rsplit("/", 1)[-1]:
        return token + ".service"
    return token


# A `systemctl` inside an `echo`/`printf`/`log` is operator guidance printed at
# the end of an installer ("Run now: sudo systemctl start x.service"), not
# something the script does. Six of this repo's installers carry one, and
# reading them as executions is how a guard cries wolf until it is deleted.
_QUOTED_RE = re.compile(r"\b(echo|printf|log|cat)\b")


def _started_units(script: Path) -> set[str]:
    """Units the script activates, by source text."""
    started: set[str] = set()
    for line in script.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in _START_RE.finditer(line):
            prefix = line[: match.start()]
            if _QUOTED_RE.search(prefix):
                continue
            verb = match.group("verb")
            flags = match.group("flags") or ""
            rest = match.group("rest")
            if verb == "enable" and "--now" not in flags and "--now" not in rest:
                continue
            for token in rest.split():
                unit = _normalise(token)
                if unit:
                    started.add(unit)
    return started


def _start_closure(unit: str) -> set[str]:
    """`unit` plus everything starting it pulls in, per this repo's units."""
    seen: set[str] = set()
    stack = [unit]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        sections = UNITS.get(current)
        if sections is None:
            continue
        for key in _DEP_KEYS:
            for dep in _directive(sections.get("Unit", []), key):
                if dep.endswith((".service", ".timer")) and dep not in seen:
                    stack.append(dep)
    return seen


def _install_paths() -> list[Path]:
    paths = sorted(INFRA.glob("install-*.sh"))
    paths.append(INFRA / "deploy-on-merge.sh")
    return [p for p in paths if p.exists()]


def _may_start(unit: str) -> bool:
    sections = UNITS.get(unit, {})
    return "yes" in _directive(sections.get("Unit", []), "X-InstallMayStart")


def test_the_repo_still_has_scheduled_workloads_to_protect():
    """Guard against the guard silently covering nothing."""
    workloads = _scheduled_workloads()
    assert "morning-signal-bakeoff.service" in workloads, workloads
    assert "morning-signal.service" in workloads, workloads
    assert len(workloads) >= 8, workloads


def test_install_paths_are_parsed_at_all():
    """Guard against a regex that matches nothing reporting a clean sweep."""
    paths = _install_paths()
    assert len(paths) >= 10, paths
    assert any(_started_units(p) for p in paths)
    installer = INFRA / "install-morning-signal.sh"
    assert "morning-signal.timer" in _started_units(installer)


@pytest.mark.parametrize("script", _install_paths(), ids=lambda p: p.name)
def test_no_install_path_starts_a_scheduled_workload(script: Path):
    workloads = _scheduled_workloads()
    offenders: dict[str, set[str]] = {}
    for unit in _started_units(script):
        pulled = _start_closure(unit) & workloads
        pulled = {u for u in pulled if not _may_start(u)}
        if pulled:
            offenders[unit] = pulled
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
    declared = {u for u in UNITS if _may_start(u)}
    assert declared, "the escape hatch has no users — did the key get renamed?"
    started: set[str] = set()
    for script in _install_paths():
        for unit in _started_units(script):
            started |= _start_closure(unit)
    unused = declared - started
    assert not unused, (
        f"X-InstallMayStart=yes declared on {sorted(unused)} but no install or "
        "deploy path starts it. Remove the declaration."
    )
