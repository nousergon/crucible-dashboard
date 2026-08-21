#!/usr/bin/env python3
"""Render budget.yaml into a shell-sourceable manifest for box_health.sh.

WHY A GENERATED MANIFEST RATHER THAN box_health.sh PARSING YAML
---------------------------------------------------------------
box_health.sh is the box's watchdog. It must keep working when the box is
degraded -- which is exactly when a Python venv, a YAML library, or a slow
interpreter start is least trustworthy. So the watchdog stays pure bash and
sources a flat file, and the YAML parsing happens here, at install time.

WHY budget.yaml IS THE SOURCE
-----------------------------
It already enumerates every long-running service on the box. Before this, the
watchdog kept its OWN hand-maintained list, and the two drifted exactly as you
would expect: on 2026-07-27 the installed box_health.sh listed 8 services while
the copy in git listed 5, and NEITHER covered nginx -- the single ingress for
all ten vhosts. Six of fourteen services were unmonitored, and the failure
presented as green.

One list. Adding a service to the budget adds it to the watchdog.

WHY THE INSTALLED .timer FILES ARE A SECOND SOURCE (alpha-engine-config-I8034)
------------------------------------------------------------------------------
budget.yaml lives in THIS repo, and this repo installs only some of the box's
timers. `nous-ergon-ops`, `metron` and `the-cyphering` install others, and a
timer arriving from one of those repos could not carry its dead-man threshold
with it -- the row had to be hand-added here afterwards, by someone who noticed
the box's `notice: timer has no dead-man threshold` line.

Nobody reliably did. The finding has been raised, fixed by hand, and raised
again for a DIFFERENT timer six times: 2026-07-29 metron-intraday.timer,
2026-08-08 three dashboard timers at once (config-I6657), 2026-08-20
emit-service-memory.timer, and 2026-08-21 litellm-config-reconcile.timer and
llm-capability-probe.timer together, hours after nous-ergon-ops-PR809 armed
them. `tests/test_every_installed_timer_has_a_deadman_row.py` blocks the
in-repo case in CI and says in its own SCOPE paragraph that it cannot see the
others.

So a timer may now declare its own threshold, in its own unit file, as an
`X-DeadManStaleness=` key under `[Unit]`. systemd accepts and ignores `X-`
keys, so this is inert to the scheduler and travels with the file into
whatever repo installs it. The registration stops being a second edit in a
second repo that someone has to remember.

PRECEDENCE, and why this direction: **budget.yaml wins.** A row here is a
curated fleet-level decision carrying its rationale in prose; the unit key is
the unit's own claim about itself. Where both exist and DISAGREE, the manifest
takes the row and records the conflict as a comment, because a threshold that
silently changed when a sibling repo edited its unit is the drift this file
exists to prevent. Where only the key exists, it is used -- which is the whole
point.

Usage: generate-box-manifest.py [--output /etc/alpha-engine/box-services.conf]
       [--unit-dir /etc/systemd/system]
Policy: nous-ergon-ops/policies/shared-application-host-policy.md T0-4, T1-6.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

try:
    import yaml
except ImportError:
    print("generate-box-manifest: PyYAML not available", file=sys.stderr)
    sys.exit(2)

BUDGET = pathlib.Path(__file__).parent / "systemd" / "resource-limits" / "budget.yaml"
DEFAULT_OUT = "/etc/alpha-engine/box-services.conf"


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(unit: str, raw) -> int:
    """`max_staleness` (e.g. "26h", "45m", "8d") -> seconds.

    box_health.sh stays pure bash (see the module docstring), so the arithmetic
    happens here at install time and the watchdog only ever compares integers.

    Raises rather than defaulting: a threshold that silently became a wrong
    number is worse than no threshold, because it reports as covered. Bad input
    must break the install, where it is visible, not the watchdog at 03:00.
    """
    text = str(raw).strip()
    if not text or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(
            f"{unit}: max_staleness must be <integer><s|m|h|d> (got {raw!r})"
        )
    seconds = int(text[:-1]) * _UNITS[text[-1]]
    if seconds <= 0:
        raise ValueError(f"{unit}: max_staleness must be positive (got {raw!r})")
    return seconds


DEFAULT_UNIT_DIR = "/etc/systemd/system"

#: The `[Unit]` key a timer uses to declare its own dead-man threshold.
#: `X-` prefixed, so systemd parses and ignores it rather than warning.
DEADMAN_KEY = "X-DeadManStaleness"


def unit_declared_staleness(unit_dir: pathlib.Path) -> dict[str, int]:
    """Read `X-DeadManStaleness=` out of every installed *.timer.

    Deliberately a plain line scan rather than `systemctl show`: this runs at
    install time from a script that must work on a degraded box, and
    `systemctl show` does not surface unknown `X-` keys at all -- it drops them
    after parsing. The file is the only place the value exists.

    A missing directory is not an error. This generator is run from CI and from
    tests on machines with no /etc/systemd/system worth reading, and returning
    an empty mapping there is correct: budget.yaml alone is exactly the
    behaviour that existed before this key.

    A MALFORMED VALUE RAISES, matching parse_duration's contract. A threshold
    that silently became a wrong number reports as covered, which is worse than
    reporting as uncovered.
    """
    found: dict[str, int] = {}
    if not unit_dir.is_dir():
        return found
    for path in sorted(unit_dir.glob("*.timer")):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(DEADMAN_KEY):
                continue
            key, _, raw = line.partition("=")
            if key.strip() != DEADMAN_KEY:
                continue
            found[path.name] = parse_duration(path.name, raw.strip())
            break
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=DEFAULT_OUT)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument("--unit-dir", default=DEFAULT_UNIT_DIR,
                    help="where installed *.timer files live; scanned for "
                         f"{DEADMAN_KEY}= (alpha-engine-config-I8034)")
    args = ap.parse_args()

    spec = yaml.safe_load(BUDGET.read_text())
    services, ports = [], []
    # unit -> port, for the HTTP liveness probe (alpha-engine-config-I6262).
    # SERVICES and PORTS are two independent lists: `ports` skips `port: none`
    # entries, so index i of one does not necessarily describe the same
    # service as index i of the other. An alert that names a service has to
    # come from an explicit pairing, not from an assumed alignment.
    service_port: list[tuple[str, str]] = []
    for svc in spec["services"]:
        services.append(svc["unit"])
        port = svc.get("port")
        if port is None:
            # Fail loud rather than silently monitoring one fewer port.
            print(f"generate-box-manifest: {svc['unit']} has no `port:` in "
                  f"budget.yaml — add one, or set `port: none` to opt out",
                  file=sys.stderr)
            return 1
        if str(port) != "none":
            ports.append(str(port))
            service_port.append((svc["unit"], str(port)))

    excluded = [e["unit"] for e in spec.get("manifest_exclude", [])]
    # budget.yaml rows first, then any installed unit that declares its own and
    # has no row here. See the module docstring for why budget.yaml wins a
    # disagreement rather than the unit file.
    declared = {t["unit"]: parse_duration(t["unit"], t["max_staleness"])
                for t in spec.get("timers", [])}
    from_units = unit_declared_staleness(pathlib.Path(args.unit_dir))
    conflicts = sorted(u for u, secs in from_units.items()
                       if u in declared and declared[u] != secs)
    adopted = sorted(u for u in from_units if u not in declared)
    timers = sorted({**from_units, **declared}.items())
    state_paths = [e["path"] for e in spec.get("state", [])]

    body = [
        "# GENERATED by infrastructure/generate-box-manifest.py from",
        "# infrastructure/systemd/resource-limits/budget.yaml — do not hand-edit.",
        "#",
        "# Sourced by box_health.sh. Hand edits are drift: the next install",
        "# regenerates this file and silently discards them.",
        "",
        f"SERVICES=({' '.join(services)})",
        f"PORTS=({' '.join(ports)})",
        "",
        "# unit -> port, for the HTTP liveness probe (alpha-engine-config-I6262).",
        "# A listening socket is not liveness: the kernel keeps it bound while the",
        "# server behind it answers nothing, which is how vires.service stayed",
        "# 'healthy' through an 18-minute outage on 2026-08-03. box_health.sh curls",
        "# each port and treats 'no status line inside the timeout' as an outage.",
        "#",
        "# An explicit pairing, NOT SERVICES[i]/PORTS[i]: `ports` above skips",
        "# `port: none` entries, so the two lists are not index-aligned and an",
        "# alert built on that assumption would name the wrong service.",
        "declare -A SERVICE_PORT=(",
        *[f'    ["{unit}"]={port}' for unit, port in service_port],
        ")",
        "",
        "# Units deliberately outside coverage (OS plumbing, templates). box_health.sh",
        "# reports any enabled non-oneshot unit that is in neither list — so a new app",
        "# service surfaces as an alert instead of going quietly unmonitored.",
        "#",
        "# By NAME, not a count: a count cannot say what is missing, and a count-based",
        "# version of this check false-alarmed on exactly these units when first",
        "# deployed 2026-07-27.",
        f"MONITOR_EXCLUDE=({' '.join(excluded)})",
        "",
        "# Per-timer dead-man budget in SECONDS (config-I5209). Answers the",
        "# question the scheduler-state check cannot: did the job actually run,",
        "# and did it succeed? A timer failing on every fire is `active`,",
        "# `waiting`, with a valid next elapse — indistinguishable from healthy",
        "# without this. box_health.sh names any enabled timer missing a row.",
        "#",
        "# Merged from TWO sources (alpha-engine-config-I8034): budget.yaml::timers,",
        "# and `X-DeadManStaleness=` declared in an installed *.timer's own [Unit]",
        "# section — so a timer installed by nous-ergon-ops, metron or the-cyphering",
        "# carries its threshold with it instead of needing a second edit in this",
        "# repo that nobody remembers to make. budget.yaml wins a disagreement.",
        f"#   adopted from unit files: {' '.join(adopted) if adopted else '(none)'}",
        *([f"#   CONFLICT (budget.yaml won): {' '.join(conflicts)}"] if conflicts else []),
        "declare -A TIMER_MAX_STALENESS=(",
        *[f'    ["{unit}"]={secs}' for unit, secs in timers],
        ")",
        "",
        "# Durable state declared in budget.yaml::state[] (T1-4). box_health.sh",
        "# names any on-disk database NOT matching one of these, because",
        "# undeclared state is neither backed up nor knowingly accepted as lost —",
        "# it is simply unconsidered, which is what T1-4 calls a defect.",
        "# Entries may be globs (e.g. /home/ec2-user/*/.env).",
        f"STATE_DECLARED=({' '.join(state_paths)})",
        "",
    ]
    text = "\n".join(body)

    if args.stdout:
        print(text, end="")
        return 0

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out} ({len(services)} services, {len(ports)} ports)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
