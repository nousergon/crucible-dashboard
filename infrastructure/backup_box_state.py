#!/usr/bin/env python3
"""Replicate the box's declared durable state to S3 (T1-4).

WHY THIS IS NOT `aws s3 cp`
---------------------------
Every artifact here is a live SQLite database. Copying the file byte-for-byte
while a writer holds it produces a copy that may need WAL recovery to open —
crash-consistent, not application-consistent. That is also why the nightly EBS
snapshot, on its own, is not a sufficient answer to T1-4: it protects the disk,
not the database.

`sqlite3.Connection.backup()` is SQLite's online backup API. It takes a read
lock, walks the pages, and produces a file that opens cleanly with no recovery
step — while the service keeps serving. No `sqlite3` CLI needed (it is not
installed on this box) and no service downtime.

WHAT IT WILL NOT DO
-------------------
It does not decide what to back up. The inventory is `budget.yaml::state[]`,
the same registry that drives the watchdog's service, port and timer coverage.
Adding a database to the box without declaring it there means it is not backed
up AND `box_health.sh` names it as undeclared — which is the intended pairing:
the registry is the only way in, and omission is loud.

Destination: s3://alpha-engine-research/dashboard/box-state/<name>/<date>.sqlite
The instance role can PutObject there but NOT DeleteObject (verified
2026-07-28). Backups are therefore append-only from the box's perspective; a
compromised or malfunctioning box cannot destroy its own history. Retention is
an S3 lifecycle concern, deliberately not this script's.

Usage:  backup_box_state.py [--dry-run] [--budget PATH]
Policy: nous-ergon-ops/policies/shared-application-host-policy.md T1-4
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date

try:
    import yaml
except ImportError:
    print("backup_box_state: PyYAML not available", file=sys.stderr)
    sys.exit(2)

BUDGET = pathlib.Path(__file__).parent / "systemd" / "resource-limits" / "budget.yaml"
S3_PREFIX = "s3://alpha-engine-research/dashboard/box-state"


def declared_replicate(budget_path: pathlib.Path) -> list[dict]:
    """Entries the registry says to replicate. Raises rather than defaulting.

    A malformed registry must break the backup loudly at its own step, not
    quietly reduce the set of things being protected — a backup that silently
    covers less than it did yesterday is the failure this whole arc is about.
    """
    spec = yaml.safe_load(budget_path.read_text())
    entries = spec.get("state")
    if not entries:
        raise SystemExit("backup_box_state: budget.yaml declares no state[] — refusing to run")
    out = []
    for e in entries:
        if e.get("disposition") != "replicate":
            continue
        if not e.get("path"):
            raise SystemExit(f"backup_box_state: state entry missing path: {e}")
        out.append(e)
    return out


def snapshot_sqlite(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Application-consistent copy via SQLite's online backup API."""
    # URI mode with immutable=0 + read-only: we never write to the source, and
    # opening read-only means a bug here cannot corrupt a live database.
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dst)
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget", default=str(BUDGET))
    args = ap.parse_args()

    stamp = date.today().isoformat()
    failures = []
    copied = 0

    for entry in declared_replicate(pathlib.Path(args.budget)):
        src = pathlib.Path(entry["path"])
        name = src.stem
        dest = f"{S3_PREFIX}/{name}/{stamp}.sqlite"

        if not src.is_file():
            # Declared but absent is a REGISTRY defect, not a no-op. Either the
            # service moved its database or the row is stale; both need a human.
            failures.append(f"{src}: declared replicate but does not exist")
            continue

        if args.dry_run:
            print(f"would back up {src} -> {dest}")
            copied += 1
            continue

        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = pathlib.Path(td) / f"{name}.sqlite"
                snapshot_sqlite(src, tmp)
                subprocess.run(
                    ["aws", "s3", "cp", str(tmp), dest, "--only-show-errors"],
                    check=True, capture_output=True, text=True, timeout=600,
                )
            print(f"backed up {src} -> {dest} ({src.stat().st_size} bytes source)")
            copied += 1
        except Exception as exc:  # noqa: BLE001 — every failure is reported below
            detail = getattr(exc, "stderr", "") or str(exc)
            failures.append(f"{src}: {detail.strip()[:200]}")

    # Fail loud and non-zero. The unit is timer-driven, so a non-zero exit is
    # what box_health.sh's timer dead-man (config-I5209) reports as a failing
    # job — which is the only reason a silently-degrading backup would ever be
    # noticed. Partial success is still a failure: N-1 of N databases protected
    # is not "mostly backed up", it is one database unprotected.
    for f in failures:
        print(f"backup_box_state: FAILED {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"backup_box_state: {copied} database(s) replicated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
