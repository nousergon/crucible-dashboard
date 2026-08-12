"""A timer that declared Persistent=false must not pull its own service in.

WHY
---
`Requires=<own service>` in a TIMER unit's [Unit] section is not timer wiring.
It is an ordinary unit dependency, so systemd starts the named service whenever
the TIMER is started -- at install, at every boot, and after any daemon-reload
followed by a restart. The schedule reaches the service through `Unit=` under
[Timer] and needs no help from [Unit].

On an idempotent reporter that extra run is harmless, which is why most of this
box's timers carry the line and should keep it. It is an outage generator on a
unit whose run has a cost, and `Persistent=false` is exactly how such a unit
declares that a run it did not schedule must never happen. A timer may hold at
most one of the two.

Measured instance, 2026-08-12: nous-ergon-ops's router-degraded-mode-drill.timer
carried both. Installing it started the drill one second later, inside the
preopen window -- a drill whose job is to STOP the fleet's model router. Only
the script's own runtime exclusion guard kept the router up. The unit's own
comment block explained why Persistent=false was a safety property, three lines
above the directive that defeated it.

Source-text assertions on committed unit files: the units run as root on an EC2
box and executing systemd in CI is not meaningful. What this pins is the
contract.
"""

import configparser
from pathlib import Path

SYSTEMD = Path(__file__).resolve().parents[1] / "infrastructure" / "systemd"


def _unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False)
    cp.optionxform = str
    cp.read_string(path.read_text())
    return cp


def test_a_non_persistent_timer_does_not_start_its_service_when_the_timer_starts():
    checked = []
    for path in sorted(SYSTEMD.glob("*.timer")):
        cp = _unit(path)
        if cp["Timer"].get("Persistent", "true").strip().lower() != "false":
            continue
        checked.append(path.name)
        triggered = cp["Timer"].get("Unit", path.with_suffix(".service").name)
        pulled = " ".join(
            cp["Unit"].get(key, "") for key in ("Requires", "Wants", "BindsTo")
        )
        assert triggered not in pulled, (
            f"{path.name} declares Persistent=false but names {triggered} in its "
            f"[Unit] dependencies, which starts it at every timer activation -- "
            f"including at boot, which is the case Persistent=false exists to "
            f"prevent."
        )
    assert checked, (
        "No Persistent=false timer found in infrastructure/systemd/. This "
        "assertion is here so the test cannot pass vacuously if the units move."
    )
