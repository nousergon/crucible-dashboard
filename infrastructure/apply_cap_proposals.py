#!/usr/bin/env python3
"""Apply a `check_memory_budget.py --propose-caps` JSON set to budget.yaml.

This is the ACT half of alpha-engine-config-I7291. The measurement and the
derivation happen on the box (`--propose-caps`, which writes nothing); this
runs in CI, edits `budget.yaml`, and `propose-memory-caps.yml` opens the PR.
The merge stays human — see `auto-merge-policy.md`.

WHY A LINE EDITOR AND NOT PyYAML
--------------------------------
budget.yaml is ~600 lines of which the overwhelming majority are COMMENTS and
`note:` blocks, and those notes are the file's institutional memory: why
llm-egress-proxy must not be lowered, which measurement each number came from,
which incident produced the bound. `yaml.safe_load` + `yaml.dump` round-trips
the data and destroys every one of them. A proposal loop whose first act is to
delete the reasoning behind the numbers it is changing would be a net loss even
when its arithmetic is right.

So this edits the two scalar lines in place and leaves every byte it was not
asked to change exactly where it was. The edit is asserted line-precise by
`test_cap_proposals.py`, which round-trips the REAL budget.yaml and diffs it.

IDEMPOTENCE
-----------
Applying the same proposal set twice produces no second diff: the second run
finds the declared value already equal to the proposed one and skips the unit,
note included. That matters because the workflow is scheduled — a re-run
against an unmerged PR branch must not stack duplicate note paragraphs.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import sys

#: Matches `  - unit: name.service` — the start of a service block.
_UNIT_RE = re.compile(r"^(\s*)-\s+unit:\s*(\S+)\s*$")
#: Matches `    memory_high: 280M` / `    memory_max: 380M`.
_CAP_RE = re.compile(r"^(\s*)(memory_high|memory_max):\s*(\S+)\s*$")
#: Matches the opening line of a block-scalar note (`note: >`, `note: >-`, …).
_NOTE_RE = re.compile(r"^(\s*)note:\s*[>|][-+]?\s*$")


def _block_bounds(lines: list[str], unit: str) -> tuple[int, int]:
    """[start, end) line indices of one unit's block. Raises if absent.

    Raising rather than returning None is deliberate: a proposal naming a unit
    budget.yaml does not have means the box and the repo disagree about what is
    installed, which is a finding, not a line to skip quietly.
    """
    start = None
    for i, line in enumerate(lines):
        m = _UNIT_RE.match(line)
        if m and m.group(2) == unit:
            start = i
            break
    if start is None:
        raise KeyError(f"budget.yaml has no service block for {unit}")
    for j in range(start + 1, len(lines)):
        if _UNIT_RE.match(lines[j]):
            return start, j
    return start, len(lines)


def _note_paragraph(rec: dict, date: str, indent: str) -> list[str]:
    """The dated paragraph recording WHY this number moved, in the unit's note."""
    body = (
        f"{date} (automated, alpha-engine-config-I7291): "
        f"{rec['declared_high_mb']}M/{rec['declared_max_mb']}M -> "
        f"{rec['proposed_high_mb']}M/{rec['proposed_max_mb']}M. Derived, not "
        f"estimated: memory.peak {rec['peak_mb']} MiB measured over "
        f"{rec['uptime_days']} days of unit uptime, memory_max at "
        f"2.4x that peak and memory_high at 70% of max. If this number looks "
        f"wrong, the measurement is the thing to argue with — see "
        f"check_memory_budget.py's proposal block for why those multiples."
    )
    wrapped: list[str] = []
    line = indent
    for word in body.split():
        if len(line) + len(word) + 1 > 78 and line.strip():
            wrapped.append(line.rstrip())
            line = indent
        line += ("" if line == indent else " ") + word
    wrapped.append(line.rstrip())
    return [ln + "\n" for ln in wrapped] + ["\n"]


def apply_records(text: str, records: list[dict], *, date: str) -> tuple[str, list[str]]:
    """Return (new budget.yaml text, list of units actually changed)."""
    lines = text.splitlines(keepends=True)
    changed: list[str] = []

    for rec in records:
        if rec.get("status") != "propose":
            continue
        unit = rec["unit"]
        start, end = _block_bounds(lines, unit)
        want = {
            "memory_high": f"{rec['proposed_high_mb']}M",
            "memory_max": f"{rec['proposed_max_mb']}M",
        }
        edits = 0
        note_indent = None
        note_at = None
        for i in range(start, end):
            m = _CAP_RE.match(lines[i])
            if m and m.group(2) in want and m.group(3) != want[m.group(2)]:
                lines[i] = f"{m.group(1)}{m.group(2)}: {want[m.group(2)]}\n"
                edits += 1
            n = _NOTE_RE.match(lines[i])
            if n is not None:
                note_at, note_indent = i, n.group(1) + "  "
        if not edits:
            # Already at the proposed values — the idempotence case.
            continue
        if note_at is None:
            # No note block yet: open one directly after the block's last
            # scalar, at the same indentation as memory_max.
            last = max(i for i in range(start, end) if _CAP_RE.match(lines[i]))
            indent = _CAP_RE.match(lines[last]).group(1)
            para = _note_paragraph(rec, date, indent + "  ")
            lines[last + 1:last + 1] = [f"{indent}note: >-\n", *para[:-1]]
        else:
            lines[note_at + 1:note_at + 1] = _note_paragraph(rec, date, note_indent)
        changed.append(unit)

    return "".join(lines), changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=pathlib.Path,
                    default=pathlib.Path(__file__).parent / "systemd" / "resource-limits" / "budget.yaml")
    ap.add_argument("--proposals", type=pathlib.Path,
                    help="proposal JSON file; reads stdin when omitted")
    ap.add_argument("--date", default=None,
                    help="date stamped into the note (default: today, UTC). Passed "
                         "explicitly by the workflow so a re-run is reproducible.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the units that would change and write nothing")
    args = ap.parse_args()

    raw = args.proposals.read_text() if args.proposals else sys.stdin.read()
    payload = json.loads(raw)
    records = payload["records"]
    date = args.date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    # Report the holds too, on stdout, so the workflow log says what was NOT
    # examined and why. A run that proposes nothing because every unit was
    # freshly restarted must not read the same as a run that found the caps
    # correct.
    for rec in records:
        if rec["status"] != "propose":
            print(f"  {rec['status']:20s} {rec['unit']:32s} {rec.get('detail', '')[:100]}")

    text = args.budget.read_text()
    new_text, changed = apply_records(text, records, date=date)
    if not changed:
        print("no cap proposals to apply")
        return 0
    print(f"proposing: {', '.join(changed)}")
    print(f"sum(memory_max) {payload['sum_max_before_mb']} -> {payload['sum_max_after_mb']} MB "
          f"against the {payload['overcommit_bound_mb']} MB bound")
    if args.dry_run:
        return 0
    args.budget.write_text(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
