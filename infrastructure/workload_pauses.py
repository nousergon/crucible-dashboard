#!/usr/bin/env python3
"""Read the private host workload-pause declaration without evaluating shell."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import NamedTuple

PAUSE_FILE = Path("/etc/alpha-engine/workload-pauses.tsv")
_UNIT = re.compile(
    r"[A-Za-z0-9_.][A-Za-z0-9_.-]*(?:@[A-Za-z0-9_.-]+)?\.(?:service|timer)",
    re.ASCII,
)
_PORT = re.compile(r"[1-9][0-9]{0,4}", re.ASCII)


class WorkloadPause(NamedTuple):
    port: int | None
    provenance: str
    reason: str


def load_workload_pauses(
    path: Path = PAUSE_FILE, *, expected_uid: int = 0, required: bool = False
) -> dict[str, WorkloadPause]:
    """An absent declaration is empty. Reject unsafe or partial declarations."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise
        return {}

    pauses: dict[str, WorkloadPause] = {}
    ports: set[int] = set()
    with os.fdopen(fd, encoding="utf-8") as declaration:
        info = os.fstat(declaration.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid:
            raise ValueError(f"{path}: must be a regular file with the expected owner")
        if info.st_mode & 0o022:
            raise ValueError(f"{path}: must not be group- or world-writable")

        for number, raw in enumerate(declaration, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            location = f"{path} line {number}"
            fields = line.split("\t")
            if len(fields) != 4:
                raise ValueError(f"{location}: expected unit, port, provenance and reason")
            unit, raw_port, provenance, reason = fields
            if len(unit) > 255 or not _UNIT.fullmatch(unit) or unit in pauses:
                raise ValueError(f"{location}: expected a unique, exact unit name")
            if not provenance.strip() or not reason.strip() or not all(
                value.isprintable() for value in (provenance, reason)
            ):
                raise ValueError(f"{location}: expected printable provenance and reason")

            port = None
            if raw_port != "-":
                if not unit.endswith(".service") or not _PORT.fullmatch(raw_port):
                    raise ValueError(f"{location}: expected a valid service port or '-'")
                port = int(raw_port)
                if port > 65535 or port in ports:
                    raise ValueError(f"{location}: expected a unique port from 1 to 65535")
                ports.add(port)
            pauses[unit] = WorkloadPause(port, provenance, reason)
    return pauses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=PAUSE_FILE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="require and validate the file")
    mode.add_argument("--unit", help="return 0 only if this exact unit is declared paused")
    args = parser.parse_args()
    try:
        pauses = load_workload_pauses(args.file, required=args.validate)
    except (OSError, ValueError) as error:
        print(f"workload pause declaration: {error}", file=sys.stderr)
        return 2

    if args.validate:
        return 0
    if args.unit is not None:
        return 0 if args.unit in pauses else 1
    for unit, (port, provenance, reason) in pauses.items():
        print(unit, "-" if port is None else port, provenance, reason, sep="\t")
    return 0


if __name__ == "__main__":
    sys.exit(main())
