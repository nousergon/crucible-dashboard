#!/usr/bin/env python3
"""Enforce the dashboard box's aggregate memory budget.

The invariant: the sum of every application unit's MemoryMax must fit in RAM
with a reserve left over for the kernel and page cache.

Per-service caps bound ONE runaway process. They do nothing when several
services grow toward their individually-legal ceilings at once -- the kernel
OOM killer then fires against whatever it picks, which need not be the service
that caused the pressure. On 2026-07-27 the sum stood at ~4650 MB against
1913 MB of RAM (2.4x) and the box cascaded into an unmanageable state.

Two modes:

  --declared   Check budget.yaml against itself (no systemd needed). This is
               the CI mode: it catches a service being added to the budget
               that pushes the sum over the ceiling, before it ever ships.

  --installed  Check what systemd has ACTUALLY loaded, and additionally assert
               that every unit in budget.yaml is installed with the values the
               budget declares. This is the on-box mode, run by box_health.sh.
               It catches drift -- a hand-edited drop-in, a unit whose limits
               were never installed, or a service running with no cap at all.

Exit 0 = within budget and (in --installed mode) no drift. Exit 1 = violation.

Policy: nous-ergon-ops/policies/shared-application-host-policy.md T1-1.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced loudly, never swallowed
    print("check_memory_budget: PyYAML not available", file=sys.stderr)
    sys.exit(2)

BUDGET = pathlib.Path(__file__).parent / "systemd" / "resource-limits" / "budget.yaml"

# Module constants rather than literals inside the readers, so tests can point
# them at a fixture tree. A check that can only be exercised on the live box is
# a check that never gets exercised.
_CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup/system.slice")

_SUFFIX = {"K": 1024, "M": 1024**2, "G": 1024**3}


def parse_bytes(value: str) -> int:
    """Parse a systemd size string (250M, 1G, 1048576) into bytes."""
    value = str(value).strip()
    if value in ("infinity", ""):
        # An uncapped service is the thing this check exists to catch. Treat it
        # as infinite so it can never silently pass the sum.
        return sys.maxsize
    if value[-1].upper() in _SUFFIX:
        return int(float(value[:-1]) * _SUFFIX[value[-1].upper()])
    return int(value)


def systemd_show(unit: str, prop: str) -> str:
    r = subprocess.run(
        ["systemctl", "show", unit, "-p", prop, "--value"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"systemctl show {unit} {prop} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def cgroup_value(unit: str, filename: str) -> int | None:
    """Read one integer from a unit's cgroup v2 file. None if unreadable.

    None is a legitimate answer -- a unit that is not running has no cgroup --
    and is handled explicitly at every call site rather than defaulted to a
    number, which would make a missing reading look like a passing one.
    """
    p = _CGROUP_ROOT / unit / filename
    try:
        raw = p.read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return sys.maxsize
    try:
        return int(raw)
    except ValueError:
        return None


def censored_observation(unit: str, declared_observed_mb: int) -> str | None:
    """Detect an observed_mb that is a FLOOR rather than a measurement.

    `memory.peak >= memory.high` means the cgroup has been held at its soft cap
    since it last started. Everything measured in that state is bounded by the
    cap itself, so the number recorded in budget.yaml is the largest value the
    ceiling permitted -- not the service's working set.

    This tell has been found BY HAND three times on this box (litellm-proxy,
    metron-api/config-I5216, dashboard/config-I5237) and written into a prose
    `note:` each time instead of a check. Each time, the cap was then raised to
    just above the censored floor and the service re-pinned within a day,
    because the floor was never the working set. config-I5237 raised
    dashboard.service 210M -> 260M and it was throttling again the next day.

    It matters beyond one service: `max_steady_state_fraction` -- the bound this
    file calls the one that governs normal operation -- is computed by summing
    observed_mb. Censored inputs make that bound read safer than it is.
    """
    peak = cgroup_value(unit, "memory.peak")
    high = cgroup_value(unit, "memory.high")
    if peak is None or high is None or high == sys.maxsize:
        return None
    if peak < high:
        return None
    return (
        f"{unit}: CENSORED observation -- memory.peak ({peak // 1024**2} MiB) has "
        f"reached memory.high ({high // 1024**2} MiB), so this service has been "
        f"pinned at its soft cap since it last started. observed_mb "
        f"({declared_observed_mb} MB) is a FLOOR, not a working set. Raise the "
        f"cap clear of the service, let it run un-pinned, and re-measure -- do "
        f"NOT re-cap to just above this number."
    )


def stale_observation(unit: str, declared_observed_mb: int) -> str | None:
    """Detect an observed_mb that the live reading has left behind.

    Distinct from censoring: here the cap is NOT binding, the service is simply
    using materially more than the file claims. That silently understates
    max_steady_state_fraction in the same way, without the peak==high tell.

    20% tolerance because observed_mb is a steady-state figure and normal
    operation moves around it; this is meant to catch a number that is wrong,
    not one that is imprecise.
    """
    current = cgroup_value(unit, "memory.current")
    if current is None or declared_observed_mb <= 0:
        return None
    current_mb = current // 1024**2
    if current_mb <= declared_observed_mb * 1.20:
        return None
    return (
        f"{unit}: STALE observation -- budget.yaml declares observed_mb="
        f"{declared_observed_mb} MB but the cgroup currently holds {current_mb} "
        f"MB ({current_mb / declared_observed_mb:.1f}x). The steady-state bound "
        f"is the sum of these values, so it is being computed from a number the "
        f"box disagrees with."
    )


# Memory drop-ins that legitimately exist without a budget.yaml entry.
# BY NAME WITH A REASON, for the same argument the manifest exclusions carry: a
# bare "ignore anything unexpected" cannot say what it is ignoring.
DROPIN_ALLOW = {
    # timer-driven, out of this budget's scope, already tracked in VCS at
    # infrastructure/systemd/morning-signal.service.d/10-memory.conf
    "morning-signal.service",
}

_DROPIN_ROOT = pathlib.Path("/etc/systemd/system")


def orphan_dropins(budget_units: set[str]) -> list[str]:
    """Find installed memory drop-ins that no budget entry owns.

    install-resource-limits.sh only ever visits units listed in budget.yaml, so
    a drop-in belonging to a unit that is NOT listed is never inspected, never
    cleaned up, and never checked -- it is invisible to the whole mechanism
    while still being live systemd config.

    Found on 2026-07-28: alpha-engine-dashboard.service.d/memory-limit.conf,
    setting MemoryMax=300M/MemoryHigh=250M for a unit that does not exist at all
    (`systemctl is-enabled` -> "No such file or directory"). Harmless in that
    instance only because the unit is absent; the same gap would just as happily
    hide a stale cap on a unit that IS running, which is precisely the
    "hand-edited drop-in" class this script's docstring claims to catch.
    """
    found = []
    for conf in sorted(_DROPIN_ROOT.glob("*.service.d/*.conf")):
        try:
            body = conf.read_text()
        except OSError:
            continue
        if "MemoryMax=" not in body and "MemoryHigh=" not in body:
            continue
        unit = conf.parent.name[: -len(".d")]
        if unit in budget_units or unit in DROPIN_ALLOW:
            continue
        unit_exists = (_DROPIN_ROOT / unit).exists() or any(
            pathlib.Path(d, unit).exists()
            for d in ("/usr/lib/systemd/system", "/lib/systemd/system")
        )
        found.append(
            f"ORPHAN drop-in {conf}: sets memory limits for {unit}, which has no "
            f"budget.yaml entry"
            + ("" if unit_exists else " and no unit file on disk")
            + ". Either add it to the budget or delete the drop-in -- an "
            "unowned limit is live config that nothing reviews."
        )
    return found


def ram_mb_from_proc() -> int:
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemTotal not found in /proc/meminfo")


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--declared", action="store_true",
                      help="check budget.yaml against itself (CI mode)")
    mode.add_argument("--installed", action="store_true",
                      help="check systemd's loaded values and assert no drift (on-box mode)")
    ap.add_argument("--quiet", action="store_true", help="only print on failure")
    args = ap.parse_args()
    installed = args.installed and not args.declared

    spec = yaml.safe_load(BUDGET.read_text())
    reserve = float(spec["reserve_fraction"])

    # In --installed mode the real RAM is authoritative: it catches a resize
    # that nobody reflected back into budget.yaml, in either direction.
    if installed:
        ram_mb = ram_mb_from_proc()
        declared_ram = int(spec["ram_mb"])
        if abs(ram_mb - declared_ram) > declared_ram * 0.05:
            print(f"DRIFT: budget.yaml declares ram_mb={declared_ram} but the box "
                  f"has {ram_mb} MB. Re-budget after an instance resize.",
                  file=sys.stderr)
            return 1
    else:
        ram_mb = int(spec["ram_mb"])

    ceiling_mb = int(ram_mb * (1 - reserve))

    total_bytes = 0
    rows, problems = [], []

    for svc in spec["services"]:
        unit = svc["unit"]
        want = parse_bytes(svc["memory_max"])

        if installed:
            try:
                have = parse_bytes(systemd_show(unit, "MemoryMax"))
                have_high = parse_bytes(systemd_show(unit, "MemoryHigh"))
            except RuntimeError as e:
                problems.append(f"{unit}: {e}")
                continue
            if have != want:
                problems.append(
                    f"{unit}: MemoryMax drift -- budget declares "
                    f"{svc['memory_max']} but systemd has "
                    f"{'infinity' if have == sys.maxsize else str(have // 1024**2) + 'M'}"
                )
            if have_high == sys.maxsize:
                problems.append(
                    f"{unit}: MemoryHigh is unset/infinity -- no reclaim window "
                    f"before the hard cap"
                )
            # The declared numbers can be internally consistent and still be
            # wrong about the box. These two catch that.
            for check in (censored_observation, stale_observation):
                found = check(unit, int(svc["observed_mb"]))
                if found:
                    problems.append(found)
            effective = have
        else:
            effective = want

        total_bytes += effective
        rows.append((unit, effective))

    if installed:
        problems.extend(orphan_dropins({s["unit"] for s in spec["services"]}))

    total_mb = total_bytes // 1024**2 if total_bytes < sys.maxsize else -1

    # Bound 1: bounded overcommit. Caps are sized for STARTUP PEAKS, which do
    # not coincide across services, so the sum is allowed to exceed the ceiling
    # by a declared ratio -- but only by a declared one. An unbounded sum is the
    # 2.4x accident that cascaded the box on 2026-07-27.
    max_ratio = float(spec["max_overcommit_ratio"])
    allowed_mb = int(ceiling_mb * max_ratio)
    ratio = (total_mb / ceiling_mb) if ceiling_mb and total_mb > 0 else float("inf")
    over = total_mb > allowed_mb or total_mb < 0

    # Bound 2: steady-state safety. This is the one that governs normal
    # operation -- the sum of what the services ACTUALLY use must leave real
    # headroom, independent of how generous the caps are.
    max_ss = float(spec["max_steady_state_fraction"])
    ss_mb = sum(int(s["observed_mb"]) for s in spec["services"])
    ss_allowed = int(ram_mb * max_ss)
    ss_over = ss_mb > ss_allowed

    if not args.quiet or over or ss_over or problems:
        label = "installed" if installed else "declared"
        print(f"memory budget ({label}): RAM {ram_mb} MB, reserve {reserve:.0%}, "
              f"ceiling {ceiling_mb} MB, max overcommit {max_ratio:.2f}x "
              f"(= {allowed_mb} MB)")
        for unit, b in sorted(rows, key=lambda r: -r[1]):
            print(f"  {unit:<28} {b // 1024**2:>5} MB")
        print(f"  {'TOTAL (caps)':<28} {total_mb:>5} MB  "
              f"{ratio:.2f}x ceiling "
              f"({'OVER' if over else 'within declared overcommit'})")
        print(f"  {'TOTAL (steady state)':<28} {ss_mb:>5} MB  "
              f"{ss_mb / ram_mb:.0%} of RAM "
              f"({'OVER' if ss_over else 'ok'}, limit {max_ss:.0%})")

    for p in problems:
        print(f"DRIFT: {p}", file=sys.stderr)

    if over:
        print(f"FAIL: aggregate MemoryMax {total_mb} MB is {ratio:.2f}x the "
              f"{ceiling_mb} MB ceiling, above the declared "
              f"max_overcommit_ratio {max_ratio:.2f}x. Either lower a cap, raise "
              f"the ratio WITH a written rationale, or move a service off this "
              f"box (policy T1-1 / decision framework section 4).", file=sys.stderr)
    if ss_over:
        print(f"FAIL: steady-state total {ss_mb} MB is {ss_mb / ram_mb:.0%} of "
              f"RAM, above the {max_ss:.0%} limit. This is the bound that governs "
              f"normal operation -- the box is genuinely too small for what it "
              f"runs (policy T1-7 / exit trigger E3).", file=sys.stderr)
    return 1 if (over or ss_over or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
