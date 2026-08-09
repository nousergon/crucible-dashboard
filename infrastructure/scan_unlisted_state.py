#!/usr/bin/env python3
"""Find state-shaped files on the box that nobody declared (T1-4).

WHY THIS EXISTS
----------------
`backup_box_state.py` replicates every path `budget.yaml::state[]` declares.
That is the LISTED half of shared-application-host-policy.md §5 T1-4. The
clause's operative word is *unlisted*: "Every SQLite file, vault, and cert on
this box is either replicated to S3 on a schedule or documented as
accepted-loss with a stated RPO. UNLISTED STATE IS A DEFECT." Nothing on the
box scanned for state nobody had declared at all — a database or key file
could exist anywhere, backed up by nothing and accepted-loss by nothing,
because no one had DECIDED, and that is indistinguishable on disk from state
someone chose to risk. Live instance found by hand: the router-edge
credential renderer that existed only in /tmp (alpha-engine-config-I6379).

box_health.sh already carries a narrower version of this idea (grep its
"Durable-state coverage" section) — but it is scoped to /home/ec2-user,
maxdepth 4, `.db`/`.sqlite` only, and has no accepted-loss mechanism of its
own; it exists to name a hole in the BACKUP registry, not to find state
anywhere on the box. This script is the general form the issue asks for:
wider scope (/etc, /opt, /home/ec2-user, /tmp — /tmp deliberately, since
I6379 was exactly a /tmp-only artifact), wider extensions (.sqlite3, .pem,
.key, vault-shaped names), and its own accepted-loss allowlist distinct from
budget.yaml::state[] (see unlisted_state_allowlist.yaml).

THREE WAYS A FOUND FILE IS "LISTED"
------------------------------------
1. It matches a path budget.yaml::state[] declares (replicate / external /
   accepted-loss — ANY disposition counts as "considered", which is the same
   registry backup_box_state.py reads, reused rather than re-declared).
2. It matches an entry in unlisted_state_allowlist.yaml, this script's own
   accepted-loss registry for state outside the backup registry's scope,
   each entry carrying a stated RPO and rationale (T1-4's proviso).
3. It is a file `git ls-files` reports as TRACKED inside its containing
   checkout. A test fixture's `.sqlite` or a vendored `.pem` sitting in a
   repo's tracked tree is source, not box state, and flagging it would drown
   every real finding under noise from every checkout on the box.

Anything left over is unlisted, which T1-4 calls a defect: the scan exits
non-zero and pages through krepis.alerts — the same channel box_health.sh
uses — so a planted undeclared file goes critical on the next run, not on the
next person who happens to `find` it by hand.

Usage:  scan_unlisted_state.py [--dry-run] [--budget PATH] [--allowlist PATH] [--root PATH ...]
Policy: nous-ergon-ops/policies/shared-application-host-policy.md T1-4
Issue:  alpha-engine-config-I6719
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import pathlib
import shlex
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("scan_unlisted_state: PyYAML not available", file=sys.stderr)
    sys.exit(2)

_HERE = pathlib.Path(__file__).parent
BUDGET = _HERE / "systemd" / "resource-limits" / "budget.yaml"
ALLOWLIST = _HERE / "unlisted_state_allowlist.yaml"

# ── scan scope (explicit, per the issue's "make exclusions explicit "
#    "constants or the scan drowns") ────────────────────────────────────────
#
# /tmp IS in scope, deliberately: I6379 is exactly a /tmp-only artifact, and
# excluding it would make this scan blind to the incident it was built for.
SCAN_ROOTS: tuple[pathlib.Path, ...] = (
    pathlib.Path("/etc"),
    pathlib.Path("/opt"),
    pathlib.Path("/home/ec2-user"),
    pathlib.Path("/tmp"),
)

STATE_EXTENSIONS: tuple[str, ...] = (".db", ".sqlite", ".sqlite3", ".pem", ".key")

# Coarse, deliberately: "vault-shaped" has no fixed extension. A filename
# containing this token (case-insensitive) is flagged for a human to look at,
# same as any other candidate — it still has to clear the declared/allowlist/
# git-tracked filters below before it is reported as unlisted.
VAULT_NAME_TOKENS: tuple[str, ...] = ("vault",)

# Directory NAMES pruned wherever they occur. This is what stops the scan
# drowning in a checkout's dependency tree — a `.git` fixture repo, a
# `node_modules`, a `.venv` all get pruned the instant os.walk sees them,
# before a single file inside is even listed.
EXCLUDE_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".venv", "venv", ".venv-intraday", "node_modules",
    "__pycache__", ".cache", ".next", "dist", "build", "site-packages",
    ".tox", ".mypy_cache", ".pytest_cache",
})

# Absolute-path substrings pruned wherever they occur, for vendor trees that
# are not reliably excludable by directory NAME alone (an installed package's
# own directory name is not one of the generic build-artifact names above).
EXCLUDE_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/opt/aws/",
    "/opt/amazon-cloudwatch-agent/",
    "/opt/amazon/",
)

NAMESPACE = "AlphaEngine/Box"
METRIC_NAME = "UnlistedStateFiles"

# Same channel box_health.sh pages through (SNS + Telegram via krepis.alerts;
# SNS auth comes from the instance role, Telegram creds load from this env
# file). Matches box_health.sh's ENV_FILE/VENV_PY exactly.
ENV_FILE = pathlib.Path("/home/ec2-user/.alpha-engine.env")
VENV_PY = pathlib.Path("/home/ec2-user/alpha-engine-dashboard/.venv/bin/python")


def load_declared_paths(budget_path: pathlib.Path) -> list[str]:
    """Every path budget.yaml::state[] declares, any disposition.

    Reuses the same registry backup_box_state.py reads (declared_replicate
    there filters to disposition=='replicate' only; T1-4 coverage needs ALL
    three dispositions, since 'external' and 'accepted-loss' are just as much
    a considered decision as 'replicate' — the whole point is that someone
    decided, not which way).

    Raises ValueError rather than defaulting, same reasoning as
    backup_box_state.py's declared_replicate(): a malformed registry must
    break this script's own run loudly (main() turns it into exit code 2),
    not silently narrow what counts as declared. A plain exception, not
    SystemExit, so main()'s `except Exception` can actually catch it —
    SystemExit is a BaseException and would otherwise propagate straight
    through that handler.
    """
    spec = yaml.safe_load(budget_path.read_text()) or {}
    out: list[str] = []
    for entry in spec.get("state") or []:
        path = entry.get("path")
        if not path:
            raise ValueError(f"state entry missing path: {entry}")
        out.append(path)
    return out


def load_allowlist(allowlist_path: pathlib.Path) -> list[dict]:
    """This script's own accepted-loss registry, distinct from budget.yaml.

    budget.yaml::state[] backs the nightly S3 replication job and is scoped
    to what that job already knows about; this scan covers the whole box, so
    its allowlist is its own file. Absent file = empty allowlist (a fresh
    install with nothing yet dispositioned), not an error.

    Every entry needs `path`, `rpo`, and `rationale` — an allowlist entry
    missing either is indistinguishable from a hole punched to silence the
    scan, so a malformed entry breaks the run rather than being read as
    "allowed with no reason given".
    """
    if not allowlist_path.is_file():
        return []
    spec = yaml.safe_load(allowlist_path.read_text()) or {}
    entries = spec.get("entries") or []
    for entry in entries:
        missing = [k for k in ("path", "rpo", "rationale") if not entry.get(k)]
        if missing:
            raise ValueError(f"allowlist entry missing {missing}: {entry}")
    return entries


def is_state_shaped(filename: str) -> bool:
    lower = filename.lower()
    if lower.endswith(STATE_EXTENSIONS):
        return True
    return any(token in lower for token in VAULT_NAME_TOKENS)


def matches_any(path_str: str, patterns: list[str]) -> bool:
    """Glob match, mirroring box_health.sh's `case "$f" in $d|$d*)` exactly.

    A declared/allowlisted entry that names a directory (no trailing glob)
    covers everything under it — `$d*` — as well as itself — `$d`. Patterns
    are matched with fnmatchcase so a literal path with no glob characters
    still matches itself via the bare `patterns` branch.
    """
    for pattern in patterns:
        if fnmatch.fnmatchcase(path_str, pattern):
            return True
        if fnmatch.fnmatchcase(path_str, pattern.rstrip("/") + "*"):
            return True
    return False


def git_tracked_files(repo_root: pathlib.Path, git_bin: str = "git") -> set[str]:
    """Absolute paths `git ls-files` reports as tracked inside repo_root.

    Degrades to an empty set on any failure (git absent, not a repo, a
    permissions error) rather than raising — this is an ADDITIONAL exclusion
    on top of the declared/allowlist filters, not the only thing standing
    between a real finding and being reported, so failing open here costs
    nothing but a noisier (never a quieter) scan.
    """
    try:
        proc = subprocess.run(
            [git_bin, "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, timeout=30,
        )
    except Exception:
        return set()
    if proc.returncode != 0:
        return set()
    names = proc.stdout.decode("utf-8", "replace").split("\0")
    return {str(repo_root / name) for name in names if name}


def discover_git_tracked(roots: list[pathlib.Path]) -> set[str]:
    """git-tracked files across every checkout found directly under `roots`.

    Checked ONE level deep (root itself, and root's immediate children) —
    that is where every product checkout on this box lives
    (/home/ec2-user/<repo>), and going deeper would mean walking into
    .venv/node_modules trees looking for nested .git dirs that do not exist
    in this box's layout, for no benefit.
    """
    tracked: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root]
        try:
            candidates.extend(c for c in root.iterdir() if c.is_dir())
        except OSError:
            pass
        for candidate in candidates:
            if (candidate / ".git").is_dir():
                tracked |= git_tracked_files(candidate)
    return tracked


def find_state_shaped_files(
    roots: list[pathlib.Path],
    exclude_dir_names: frozenset[str] = EXCLUDE_DIR_NAMES,
    exclude_path_substrings: tuple[str, ...] = EXCLUDE_PATH_SUBSTRINGS,
) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude_dir_names)
            if any(sub in (dirpath + "/") for sub in exclude_path_substrings):
                dirnames[:] = []
                continue
            for fname in filenames:
                if is_state_shaped(fname):
                    found.append(pathlib.Path(dirpath) / fname)
    return found


def scan(
    roots: list[pathlib.Path],
    declared_patterns: list[str],
    allowlist_entries: list[dict],
    git_tracked: set[str] | None = None,
) -> list[pathlib.Path]:
    """The unlisted set: state-shaped files matching none of the three filters."""
    if git_tracked is None:
        git_tracked = discover_git_tracked(roots)
    allow_patterns = [e["path"] for e in allowlist_entries]

    unlisted: list[pathlib.Path] = []
    for candidate in find_state_shaped_files(roots):
        path_str = str(candidate)
        if path_str in git_tracked:
            continue
        if matches_any(path_str, declared_patterns):
            continue
        if matches_any(path_str, allow_patterns):
            continue
        unlisted.append(candidate)
    return sorted(unlisted)


def _instance_id() -> str:
    try:
        token = subprocess.run(
            ["curl", "-s", "--max-time", "2", "-X", "PUT",
             "http://169.254.169.254/latest/api/token",
             "-H", "X-aws-ec2-metadata-token-ttl-seconds: 60"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        instance_id = subprocess.run(
            ["curl", "-s", "--max-time", "2",
             "-H", f"X-aws-ec2-metadata-token: {token}",
             "http://169.254.169.254/latest/meta-data/instance-id"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return instance_id or "unknown"
    except Exception:
        return "unknown"


def emit_metric(count: int) -> None:
    """Publish to AlphaEngine/Box, ZEROS INCLUDED.

    Every run publishes, healthy or not — same reasoning as box_health.sh's
    emit_metrics(): a check that only speaks when something is wrong cannot be
    told apart from one that has died. Absence of this metric must never be
    the thing that reads as "clean".
    """
    instance_id = _instance_id()
    try:
        subprocess.run(
            ["aws", "cloudwatch", "put-metric-data",
             "--namespace", NAMESPACE,
             "--metric-data",
             f"MetricName={METRIC_NAME},Value={count},Unit=Count,"
             f"Dimensions=[{{Name=InstanceId,Value={instance_id}}}]"],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"scan_unlisted_state: metric publish failed: {detail}".strip(), file=sys.stderr)


def publish_alert(findings: list[pathlib.Path]) -> None:
    """Page red through krepis.alerts — the SAME channel box_health.sh uses.

    Deliberately not routed through box_health.sh's own snapshot/confirm loop
    (this script is timer-driven, not polled) — it publishes its own critical
    finding directly, the same way box-state-backup's non-zero exit is picked
    up by the systemd Result box_health.sh's timer dead-man reads. This call
    is the immediate half; a `timers:` row for scan-unlisted-state.timer in
    budget.yaml (deferred — see the PR body) is the staleness half, so a
    stopped timer pages too, not only a failing one.
    """
    names = ", ".join(str(p) for p in findings[:10])
    if len(findings) > 10:
        names += f", +{len(findings) - 10} more"
    message = (
        f"dashboard EC2 unlisted state ({len(findings)} file(s)): {names} — "
        "declare in budget.yaml::state[] or add to "
        "unlisted_state_allowlist.yaml with a stated RPO (T1-4)"
    )
    dedup_key = f"scan-unlisted-state-{len(findings)}"
    shell_cmd = (
        f'if [ -f "{ENV_FILE}" ]; then set -a; . "{ENV_FILE}"; set +a; fi; '
        f'exec "{VENV_PY}" -m krepis.alerts publish '
        f"--message {shlex.quote(message)} --severity critical "
        f"--source scan-unlisted-state --dedup-key {shlex.quote(dedup_key)} "
        "--dedup-window-min 1440"
    )
    try:
        subprocess.run(["bash", "-c", shell_cmd], check=True, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"scan_unlisted_state: alert publish failed: {detail}".strip(), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="scan and report, but never publish a metric or an alert")
    ap.add_argument("--budget", default=str(BUDGET))
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    ap.add_argument("--root", action="append", dest="roots",
                     help="override a scan root (repeatable); default is the full SCAN_ROOTS set")
    args = ap.parse_args(argv)

    roots = [pathlib.Path(r) for r in args.roots] if args.roots else list(SCAN_ROOTS)

    try:
        declared = load_declared_paths(pathlib.Path(args.budget))
        allowlist = load_allowlist(pathlib.Path(args.allowlist))
    except Exception as exc:
        print(f"scan_unlisted_state: could not read registry/allowlist: {exc}", file=sys.stderr)
        return 2

    unlisted = scan(roots, declared, allowlist)

    if not args.dry_run:
        emit_metric(len(unlisted))

    if unlisted:
        for path in unlisted:
            print(f"scan_unlisted_state: FAILED unlisted state: {path}", file=sys.stderr)
        print(
            f"scan_unlisted_state: {len(unlisted)} unlisted state-shaped file(s) — "
            "declare in budget.yaml::state[] or add to unlisted_state_allowlist.yaml "
            "with a stated RPO (T1-4)",
            file=sys.stderr,
        )
        if not args.dry_run:
            publish_alert(unlisted)
        return 1

    print("scan_unlisted_state: 0 unlisted state-shaped files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
