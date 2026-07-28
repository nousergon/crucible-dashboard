"""Pure helpers for the PR Pipeline console page — parses the deterministic
PR-sweep's per-cycle machine counters out of ``groom/{date}/sweep-*.json``
artifacts (``run_kind == "sweep"``) into cross-run trend rows, plus the
classify-bucket counts embedded in the same digest.

Ground truth (2026-07-20 verification against 21 real sweep artifacts,
2026-07-13..2026-07-19, config#2709): the sweep loop
(``scripts/groom_run.sh``) writes ``"DONE " key=value ...`` machine lines
into ``digest_markdown`` for four families, one block per quiescence-loop
cycle within a run:

  - ``SCANNER_MERGE_SWEEP_DONE``           evaluated= merged= would_merge_if_enabled=
  - ``STANDING_EXCEPTION_MERGE_SWEEP_DONE`` evaluated= merged= would_merge_if_enabled=
  - ``GROOM_REVIEWED_MERGE_SWEEP_DONE``     evaluated= merged= approved_dry_run= blocked=
  - ``STALENESS_FLUSH_DONE``                flushed_gated= flushed_ready=
                                             linkage_violations= skipped_recent=

These four are captured verbatim per ``scripts/pr_sweep_classify.py`` /
``scripts/scanner_pr_merge_sweep.py`` / ``scripts/standing_exception_merge_sweep.py``
/ ``scripts/groom_reviewed_merge_sweep.py`` / ``scripts/pr_staleness_flush.py``
and are stable machine format by design (grepped for verbatim, not narrated
by an LLM).

**Deviation from the config#2709 issue text**: the issue names a fifth line,
``PR_SWEEP_CLASSIFY_DONE conflicts= ci_red= ... clean_ready= ...``. That
exact ``KEY=value`` line is real (``pr_sweep_classify.py`` prints it to
stdout at every quiescence-loop cycle, ``scripts/groom_run.sh`` line ~461)
but is NEVER captured into the artifact — ``groom_run.sh`` redirects that
call's stdout to a throwaway var (``CYCLE_CLASSIFY_JSON`` is a *file path*
argument, not a captured log) and only the LAST cycle's classification is
re-rendered into the digest via ``pr_sweep_classify.py --render-digest-from``,
as prose bold-header bullet sections ("**Still CONFLICTING...:** N", "**Still
CI-RED:** N", "**Clean + green + ready...:** N", etc.) — never the DONE-line
format. Confirmed absent from all 21 sampled real artifacts. The classify
bucket sizes are still fully recoverable (this module does so via
``CLASSIFY_SECTION_RE`` below) — same underlying data, prose-header surface
instead of a KEY=value line, and FINAL-cycle-only (not per-cycle) since nothing
upstream of the digest keeps interim cycles' classify counts. This module's
docstrings/tests call this out explicitly rather than pretending a
``PR_SWEEP_CLASSIFY_DONE`` line exists.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

#: One block per DONE-line family, matched across a whole digest (repeated
#: once per quiescence-loop cycle within a single sweep run). Field values
#: are alphanumeric/./-/_ tokens or True/False — matches every sample seen
#: live; a field this doesn't match is simply omitted from that row (no
#: parse ever raises).
_DONE_LINE_RE = re.compile(r"^(?P<name>[A-Z][A-Z_]*_DONE)\s+(?P<fields>.+)$", re.MULTILINE)
_FIELD_RE = re.compile(r"(\w+)=(True|False|[-\w./]+)")

#: Bold-header classify-bucket sections rendered by
#: ``pr_sweep_classify.py::render_digest_markdown`` — the FINAL cycle's
#: classification only (see module docstring). Maps a stable short key to
#: the exact section title substring so a wording tweak upstream degrades to
#: "count not found" rather than a silent mismatch elsewhere.
CLASSIFY_SECTION_TITLES: dict[str, str] = {
    "conflicts": "Still CONFLICTING",
    "ci_red": "Still CI-RED",
    "security_comments": "Unresolved SECURITY-scanner review threads",
    "draft_label_gaps": "Draft PRs handed to label-hygiene",
    "behind_updated": "Branches nudged",
    "clean_ready": "Clean + green + ready",
    "pending": "Still pending",
    "errors": "Refetch errors",
}
_CLASSIFY_SECTION_RE = re.compile(r"\*\*([^:*]+):\*\*\s*(\d+)")

#: DONE-line families this module knows how to parse into typed int fields.
#: Anything else that shows up in a digest (e.g. GATE_SWEEP_DONE,
#: DEP_SWEEP_DONE — separate pipelines, not this issue's scope) is ignored.
_KNOWN_DONE_FAMILIES: tuple[str, ...] = (
    "SCANNER_MERGE_SWEEP_DONE",
    "STANDING_EXCEPTION_MERGE_SWEEP_DONE",
    "GROOM_REVIEWED_MERGE_SWEEP_DONE",
    "STALENESS_FLUSH_DONE",
)

_INT_RE = re.compile(r"^-?\d+$")


def parse_done_lines(digest_markdown: str) -> dict[str, list[dict[str, Any]]]:
    """Parse every ``<NAME>_DONE key=value ...`` line in *digest_markdown*.

    Returns ``{family_name: [occurrence_dict, ...]}`` — a run with N
    quiescence-loop cycles has up to N occurrences per family (fewer if a
    cycle's script invocation failed non-fatally and wrote no DONE line;
    ``scripts/groom_run.sh`` treats every sweep script as best-effort).
    Only families in :data:`_KNOWN_DONE_FAMILIES` are returned.

    Value coercion is by SHAPE, not a hand-curated field allowlist (every
    DONE family observed live carries a different, evolving set of counter
    names — ``attribution_failed``/``attribution_reconciled``/``repos_failed``/
    etc. — an allowlist silently stops coercing the moment a script adds a
    new counter): ``True``/``False`` -> bool, a bare integer string -> int,
    anything else (e.g. a version tag) stays a str.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not digest_markdown:
        return out
    for m in _DONE_LINE_RE.finditer(digest_markdown):
        name = m.group("name")
        if name not in _KNOWN_DONE_FAMILIES:
            continue
        fields: dict[str, Any] = {}
        for key, val in _FIELD_RE.findall(m.group("fields")):
            if val == "True":
                fields[key] = True
            elif val == "False":
                fields[key] = False
            elif _INT_RE.match(val):
                fields[key] = int(val)
            else:
                fields[key] = val
        out.setdefault(name, []).append(fields)
    return out


def sum_done_family(occurrences: list[dict[str, Any]], field: str) -> int:
    """Sum one integer *field* across every cycle occurrence of a DONE family."""
    total = 0
    for occ in occurrences:
        v = occ.get(field)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            total += v
    return total


def parse_classify_buckets(digest_markdown: str) -> dict[str, int]:
    """Bold-header classify-bucket counts from the digest's FINAL re-classify
    section (see module docstring — this is NOT per-cycle; the digest only
    ever embeds the last cycle's classification). Missing headers are simply
    absent from the returned dict (never guessed as 0) so a renderer wording
    change is visible as "no data" rather than a silently wrong zero.
    """
    if not digest_markdown:
        return {}
    found: dict[str, int] = {}
    for m in _CLASSIFY_SECTION_RE.finditer(digest_markdown):
        title, count = m.group(1).strip(), m.group(2)
        for key, prefix in CLASSIFY_SECTION_TITLES.items():
            if title.startswith(prefix):
                found[key] = int(count)
                break
    return found


def _parse_run_start(run: dict[str, Any]) -> datetime | None:
    raw = run.get("run_start")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def sweep_cycle_row(key: str, run: dict[str, Any]) -> dict[str, Any] | None:
    """One trend row per sweep run artifact (``run_kind == "sweep"`` only),
    summing DONE-line counters across every quiescence-loop cycle embedded
    in that run's digest, plus the final-cycle classify bucket counts.

    Returns None for a non-sweep run or one with no readable ``run_start``.
    """
    if (run.get("run_kind") or "") != "sweep":
        return None
    start = _parse_run_start(run)
    if start is None:
        return None
    digest = run.get("digest_markdown") or ""
    done = parse_done_lines(digest)
    classify = parse_classify_buckets(digest)

    scanner = done.get("SCANNER_MERGE_SWEEP_DONE", [])
    standing = done.get("STANDING_EXCEPTION_MERGE_SWEEP_DONE", [])
    reviewed = done.get("GROOM_REVIEWED_MERGE_SWEEP_DONE", [])
    staleness = done.get("STALENESS_FLUSH_DONE", [])

    return {
        "key": key,
        "run_start": start,
        "cycles": max(len(scanner), len(standing), len(reviewed), len(staleness), 1),
        # Classify buckets (final cycle of the run only — see docstring).
        "conflicts": classify.get("conflicts"),
        "ci_red": classify.get("ci_red"),
        "clean_ready": classify.get("clean_ready"),
        "security_comments": classify.get("security_comments"),
        "draft_label_gaps": classify.get("draft_label_gaps"),
        "behind_updated": classify.get("behind_updated"),
        "pending": classify.get("pending"),
        "classify_errors": classify.get("errors"),
        # Merge throughput by path, summed across this run's cycles.
        "scanner_evaluated": sum_done_family(scanner, "evaluated"),
        "scanner_merged": sum_done_family(scanner, "merged"),
        "standing_evaluated": sum_done_family(standing, "evaluated"),
        "standing_merged": sum_done_family(standing, "merged"),
        "reviewed_evaluated": sum_done_family(reviewed, "evaluated"),
        "reviewed_merged": sum_done_family(reviewed, "merged"),
        "reviewed_approved_dry_run": sum_done_family(reviewed, "approved_dry_run"),
        "reviewed_blocked": sum_done_family(reviewed, "blocked"),
        # Staleness / linkage.
        "flushed_gated": sum_done_family(staleness, "flushed_gated"),
        "flushed_ready": sum_done_family(staleness, "flushed_ready"),
        "linkage_violations": sum_done_family(staleness, "linkage_violations"),
        "skipped_recent": sum_done_family(staleness, "skipped_recent"),
    }


def sweep_trend_rows(runs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """One row per sweep run artifact, oldest-first. *runs* is
    ``[(key, run_doc), ...]`` — mirrors ``groom_trends.runs_trend_rows``'s
    input shape but for ``run_kind == "sweep"`` artifacts (coverage runs
    have no PR-sweep digest and are excluded, symmetric with how
    ``runs_trend_rows`` excludes sweeps).
    """
    rows = [r for r in (sweep_cycle_row(k, run) for k, run in runs) if r is not None]
    rows.sort(key=lambda r: r["run_start"])
    return rows


def merge_throughput_by_path(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Total merges over the window, broken out by auto-merge path — the
    "merge throughput by path" deliverable. ``dependabot-native``/``docs``
    aren't separately counted anywhere in the sweep artifacts (Dependabot's
    own native auto-merge and the standing-exception docs-only carve-out
    both land inside ``STANDING_EXCEPTION_MERGE_SWEEP_DONE``'s single
    ``merged`` counter — the sweep scripts don't sub-bucket by reason), so
    ``standing-exception`` is reported as one path covering both
    dependabot-native and docs/pin-bump standing exceptions; the digest's
    per-PR ``[pin-bump]``-tagged bullet lines (not summarized here — this
    module works off the DONE counters, not the free-text bullets) are the
    finer-grained source if that split is ever needed.
    """
    return {
        "scanner": sum(r["scanner_merged"] for r in rows),
        "standing-exception": sum(r["standing_merged"] for r in rows),
        "groom-reviewed": sum(r["reviewed_merged"] for r in rows),
    }


def review_gate_verdict_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-run approved/blocked verdict counts from
    ``GROOM_REVIEWED_MERGE_SWEEP_DONE`` — the arming-decision evidence
    surface: how often the reviewed-merge gate armed (merged/approved) vs
    held (blocked) a PR.
    """
    return [
        {
            "run_start": r["run_start"],
            "merged": r["reviewed_merged"],
            "approved_dry_run": r["reviewed_approved_dry_run"],
            "blocked": r["reviewed_blocked"],
        }
        for r in rows
    ]
