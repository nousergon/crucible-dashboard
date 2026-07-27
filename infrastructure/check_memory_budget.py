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
            effective = have
        else:
            effective = want

        total_bytes += effective
        rows.append((unit, effective))

    total_mb = total_bytes // 1024**2 if total_bytes < sys.maxsize else -1
    over = total_mb > ceiling_mb or total_mb < 0

    if not args.quiet or over or problems:
        label = "installed" if installed else "declared"
        print(f"memory budget ({label}): RAM {ram_mb} MB, "
              f"reserve {reserve:.0%}, ceiling {ceiling_mb} MB")
        for unit, b in sorted(rows, key=lambda r: -r[1]):
            print(f"  {unit:<28} {b // 1024**2:>5} MB")
        print(f"  {'TOTAL':<28} {total_mb:>5} MB "
              f"({'OVER by ' + str(total_mb - ceiling_mb) + ' MB' if over else 'within budget'})")

    for p in problems:
        print(f"DRIFT: {p}", file=sys.stderr)

    if over:
        print(f"FAIL: aggregate MemoryMax {total_mb} MB exceeds the {ceiling_mb} MB "
              f"ceiling. Per-service caps do not prevent global exhaustion -- "
              f"either lower a cap or move a service off this box "
              f"(policy T1-1 / decision framework section 4).", file=sys.stderr)
    return 1 if (over or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
