"""Every krepis.alerts surface box_health.sh uses is covered by the declared floor.

WHAT THIS EXISTS TO CATCH (alpha-engine-config-I8105)
-----------------------------------------------------
crucible-dashboard#761 added the alert lifecycle to box_health.sh -- the
``--state``/``--identity-key`` arguments on publish, and the ``clear``
subcommand -- which first ship in krepis 0.59.26 (krepis#178, merged
2026-08-21 17:23 PT). The box's shared krepis venv was pinned at 0.59.22 in
nous-ergon-ops, a repo this one's merges cannot see, and that pin did not move.

From the 2026-08-22 01:41 install until 2026-08-25, every 10-minute tick on
i-09b539c844515d549::

    python -m krepis.alerts: error: unrecognized arguments:
      --state still_open --identity-key boxhealth-critical-timerfail-...
    box_health: critical publish failed        (x2 criticals + 1 warning)

``health_problems_unalerted`` sat at 2-3 for three days and the CloudWatch
backstop stayed in ALARM. Every check involved was green, including
``check-krepis-venv-drift.sh``, whose install-vs-pin comparison was satisfied
because the INSTALL matched the pin. The pin was what was wrong, and nothing
compared it to a call site. That is the blindness this file closes on the
consumer side; ``check-krepis-venv-drift.sh`` closes it on the venv side by
asserting the installed version meets the floor declared here.

The test derives the used surface from the SHIPPED script rather than a
maintained list, so a flag added tomorrow is covered by the next run rather
than by someone remembering this file exists.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "infrastructure" / "box_health.sh"
_FLOOR_FILE = _ROOT / "infrastructure" / "krepis-floor.txt"


def _floor() -> tuple[int, int, int]:
    decl = [
        ln.strip()
        for ln in _FLOOR_FILE.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert len(decl) == 1, f"expected exactly one declaration, got {decl}"
    m = re.fullmatch(r"krepis>=(\d+)\.(\d+)\.(\d+)", decl[0])
    assert m, f"floor must read `krepis>=X.Y.Z`, got {decl[0]!r}"
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def _table() -> dict[str, tuple[int, int, int]]:
    """The CALL-SITE TABLE in krepis-floor.txt, parsed from its comment block."""
    out: dict[str, tuple[int, int, int]] = {}
    for ln in _FLOOR_FILE.read_text().splitlines():
        m = re.match(r"#\s{3}(publish|clear)\s+(\S+)\s+(\d+)\.(\d+)\.(\d+)", ln)
        if not m:
            continue
        sub, surfaces, *ver = m.groups()
        version = tuple(int(v) for v in ver)
        if surfaces == "(subcommand)":
            out[sub] = version  # type: ignore[assignment]
            continue
        for surface in surfaces.split("/"):
            out[f"{sub} {surface}"] = version  # type: ignore[assignment]
    return out


def _used_surfaces() -> set[str]:
    """Every `krepis.alerts <sub>` invocation in box_health.sh and its flags.

    Comment lines are stripped first: the file discusses these flags at length,
    and a prose mention is not a call site.
    """
    lines = [
        ln for ln in _SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")
    ]
    src = "\n".join(lines)
    used: set[str] = set()
    for m in re.finditer(
        r"-m krepis\.alerts (publish|clear)((?:[^\n]*\\\n)*[^\n]*)", src
    ):
        sub, tail = m.group(1), m.group(2)
        used.add(sub)
        for flag in re.findall(r"(--[a-z][a-z-]+)", tail):
            used.add(f"{sub} {flag}")
    return used


def test_box_health_actually_invokes_krepis_alerts() -> None:
    """Guard the guard: a regex that matches nothing would pass every test below."""
    used = _used_surfaces()
    assert "publish" in used, used
    assert "clear" in used, used
    assert len(used) >= 5, used


def test_every_used_surface_is_in_the_call_site_table() -> None:
    table = _table()
    missing = sorted(s for s in _used_surfaces() if s not in table)
    assert not missing, (
        "box_health.sh uses krepis.alerts surfaces absent from the CALL-SITE "
        f"TABLE in {_FLOOR_FILE.name}: {missing}. Add each with the krepis "
        "version that first shipped it, and raise the floor if needed."
    )


def test_the_floor_is_at_least_the_highest_surface_it_must_carry() -> None:
    table = _table()
    used = _used_surfaces()
    required = max(table[s] for s in used)
    assert _floor() >= required, (
        f"krepis-floor.txt declares krepis>={'.'.join(map(str, _floor()))} but "
        f"box_health.sh uses a surface needing {'.'.join(map(str, required))}."
    )


def test_the_lifecycle_surfaces_are_capability_probed_at_the_call_site() -> None:
    """The floor stops the crash; the probe is the seatbelt. Both are required.

    Without the probe, a box below the floor fails to page at all instead of
    paging without lifecycle metadata -- which is exactly what happened for
    three days, because the `clear` path had a probe and the `publish` path
    did not.
    """
    src = _SCRIPT.read_text()
    assert "krepis_supports_clear()" in src
    assert "krepis_supports_publish_lifecycle()" in src
    body = re.search(
        r"^krepis_supports_publish_lifecycle\(\) \{$.*?^\}$", src, re.M | re.S
    )
    assert body, "krepis_supports_publish_lifecycle() not found"
    assert "inspect.signature" in body.group(0), body.group(0)
    assert "identity_key" in body.group(0), body.group(0)
    assert "state" in body.group(0), body.group(0)


def test_the_publish_call_passes_lifecycle_args_conditionally() -> None:
    """A skewed krepis must degrade the page, never fail to send it."""
    src = _SCRIPT.read_text()
    assert "krepis_publish_lifecycle_args" in src
    # Empty-array expansion must be the bash-3.2-safe form: a bare
    # "${arr[@]}" on an empty array is an unbound-variable error under `set -u`,
    # which would turn the DEGRADED path into a dead watchdog.
    assert '${_lifecycle[@]+"${_lifecycle[@]}"}' in src, src[:0]
