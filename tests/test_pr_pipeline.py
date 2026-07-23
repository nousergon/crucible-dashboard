"""Tests for loaders/pr_pipeline.py — the pure DONE-line / classify-bucket
parsers behind the PR Pipeline console page (config#2709).

Fixtures below are trimmed excerpts of REAL digest_markdown text pulled from
``s3://alpha-engine-research/groom/2026-07-19/sweep-114745.json`` and
sibling artifacts (2026-07-20 verification pass) — not hand-invented shapes.
"""

from datetime import datetime, timezone

from loaders.pr_pipeline import (
    CLASSIFY_SECTION_TITLES,
    merge_throughput_by_path,
    parse_classify_buckets,
    parse_done_lines,
    review_gate_verdict_rows,
    sum_done_family,
    sweep_cycle_row,
    sweep_trend_rows,
)

# Real classify-bucket section excerpt (bold-header prose, NOT a DONE line —
# see module docstring for why).
REAL_CLASSIFY_SECTION = """\
**Still CONFLICTING (needs manual/agent merge):** 0

**Still CI-RED:** 3
- nousergon/nousergon-data#947 (`feat/rag-manifest-public-read-access`) — drift-check

**Unresolved SECURITY-scanner review threads (GHAS/CodeQL):** 0

**Draft PRs handed to label-hygiene (label OR ready-flip):** 4

**Branches nudged (behind -> updated to base):** 1

**Clean + green + ready (no action needed):** 5

**Still pending (CI running / mergeable_state unknown):** 6

**Refetch errors (dropped this cycle, retried next):** 0
"""

# Real multi-cycle DONE-line excerpt (two quiescence-loop cycles).
REAL_DONE_LINES = """\
SCANNER_MERGE_SWEEP_DONE evaluated=8 merged=0 would_merge_if_enabled=0 enabled=True dry_run=False attribution_failed=0
STANDING_EXCEPTION_MERGE_SWEEP_DONE evaluated=5 merged=2 would_merge_if_enabled=0 enabled=True dry_run=False attribution_reconciled=0 attribution_failed=0
GROOM_REVIEWED_MERGE_SWEEP_DONE evaluated=8 merged=4 approved_dry_run=0 blocked=1 enabled=True dry_run=False attribution_failed=4
GATE_SWEEP_DONE flagged=9 unflagged=2 pr_flagged=0 pr_unflagged=0 dry_run=False
STALENESS_FLUSH_DONE flushed_gated=1 flushed_ready=0 linkage_violations=0 skipped_recent=2 flush_failed=0 repos_failed=0 dry_run=False
SCANNER_MERGE_SWEEP_DONE evaluated=5 merged=1 would_merge_if_enabled=0 enabled=True dry_run=False attribution_failed=0
STANDING_EXCEPTION_MERGE_SWEEP_DONE evaluated=4 merged=0 would_merge_if_enabled=0 enabled=True dry_run=False attribution_reconciled=0 attribution_failed=0
GROOM_REVIEWED_MERGE_SWEEP_DONE evaluated=5 merged=2 approved_dry_run=0 blocked=2 enabled=True dry_run=False attribution_failed=0
STALENESS_FLUSH_DONE flushed_gated=0 flushed_ready=1 linkage_violations=1 skipped_recent=0 flush_failed=0 repos_failed=0 dry_run=False
"""

FULL_DIGEST = (
    "## Deterministic PR sweep (config#2570)\n\nGenerated: 2026-07-19T11:37:29Z\n\n"
    + REAL_CLASSIFY_SECTION
    + "\n### Scanner-remediation auto-merge sweep\n```\n"
    + REAL_DONE_LINES
    + "\n```\n"
)


class TestParseDoneLines:
    def test_parses_each_known_family_with_typed_fields(self):
        parsed = parse_done_lines(REAL_DONE_LINES)
        assert set(parsed) == {
            "SCANNER_MERGE_SWEEP_DONE",
            "STANDING_EXCEPTION_MERGE_SWEEP_DONE",
            "GROOM_REVIEWED_MERGE_SWEEP_DONE",
            "STALENESS_FLUSH_DONE",
        }
        scanner = parsed["SCANNER_MERGE_SWEEP_DONE"]
        assert len(scanner) == 2  # two cycles
        assert scanner[0] == {
            "evaluated": 8, "merged": 0, "would_merge_if_enabled": 0,
            "enabled": True, "dry_run": False, "attribution_failed": 0,
        }
        assert scanner[1]["merged"] == 1

    def test_ignores_unknown_families(self):
        # GATE_SWEEP_DONE is a real line (separate pipeline) — must not leak in.
        parsed = parse_done_lines(REAL_DONE_LINES)
        assert "GATE_SWEEP_DONE" not in parsed

    def test_pr_sweep_classify_done_never_appears_in_real_artifacts(self):
        # Ground-truth check (config#2709 issue text names this line; it was
        # verified ABSENT from 21 real sweep artifacts 2026-07-13..07-19).
        # This fixture is built from real digest text — assert the parser
        # (correctly) finds none, documenting the discrepancy in a runnable
        # test rather than only a docstring.
        parsed = parse_done_lines(FULL_DIGEST)
        assert "PR_SWEEP_CLASSIFY_DONE" not in parsed

    def test_empty_digest_returns_empty(self):
        assert parse_done_lines("") == {}
        assert parse_done_lines(None) == {}  # type: ignore[arg-type]

    def test_boolean_fields_not_coerced_to_int(self):
        parsed = parse_done_lines(REAL_DONE_LINES)
        assert parsed["SCANNER_MERGE_SWEEP_DONE"][0]["enabled"] is True
        assert parsed["SCANNER_MERGE_SWEEP_DONE"][0]["dry_run"] is False


class TestSumDoneFamily:
    def test_sums_across_cycles(self):
        occ = parse_done_lines(REAL_DONE_LINES)["SCANNER_MERGE_SWEEP_DONE"]
        assert sum_done_family(occ, "merged") == 1  # 0 + 1
        assert sum_done_family(occ, "evaluated") == 13  # 8 + 5

    def test_skips_bool_fields(self):
        occ = parse_done_lines(REAL_DONE_LINES)["SCANNER_MERGE_SWEEP_DONE"]
        assert sum_done_family(occ, "enabled") == 0  # bools never summed

    def test_empty_occurrences(self):
        assert sum_done_family([], "merged") == 0


class TestParseClassifyBuckets:
    def test_all_known_sections_extracted(self):
        buckets = parse_classify_buckets(REAL_CLASSIFY_SECTION)
        assert buckets == {
            "conflicts": 0, "ci_red": 3, "security_comments": 0,
            "draft_label_gaps": 4, "behind_updated": 1, "clean_ready": 5,
            "pending": 6, "errors": 0,
        }

    def test_missing_section_absent_not_zero(self):
        # A section that never appears must be ABSENT from the dict, not
        # silently defaulted to 0 (the module docstring's explicit contract).
        partial = "**Still CI-RED:** 2\n"
        buckets = parse_classify_buckets(partial)
        assert buckets == {"ci_red": 2}
        assert "clean_ready" not in buckets

    def test_empty_digest(self):
        assert parse_classify_buckets("") == {}

    def test_every_declared_title_has_a_real_prefix_match(self):
        # Sanity: every title this module knows about actually matches
        # something in the real fixture (catches a silent typo drift).
        buckets = parse_classify_buckets(REAL_CLASSIFY_SECTION)
        assert set(buckets) == set(CLASSIFY_SECTION_TITLES)


class TestSweepCycleRow:
    def _run(self, **overrides):
        run = {
            "run_kind": "sweep",
            "run_start": "2026-07-19T11:37:29Z",
            "digest_markdown": FULL_DIGEST,
        }
        run.update(overrides)
        return run

    def test_sums_across_cycles_and_reads_classify(self):
        row = sweep_cycle_row("groom/2026-07-19/sweep-114745.json", self._run())
        assert row is not None
        assert row["cycles"] == 2
        assert row["scanner_evaluated"] == 13
        assert row["scanner_merged"] == 1
        assert row["standing_merged"] == 2  # 2 + 0
        assert row["reviewed_merged"] == 6  # 4 + 2
        assert row["reviewed_blocked"] == 3  # 1 + 2
        assert row["flushed_gated"] == 1
        assert row["linkage_violations"] == 1
        assert row["conflicts"] == 0
        assert row["ci_red"] == 3
        assert row["clean_ready"] == 5
        assert row["run_start"] == datetime(2026, 7, 19, 11, 37, 29, tzinfo=timezone.utc)

    def test_non_sweep_run_returns_none(self):
        assert sweep_cycle_row("k", self._run(run_kind="coverage")) is None

    def test_missing_run_start_returns_none(self):
        assert sweep_cycle_row("k", self._run(run_start=None)) is None

    def test_no_digest_still_returns_row_with_zero_counts(self):
        row = sweep_cycle_row("k", self._run(digest_markdown=""))
        assert row is not None
        assert row["scanner_merged"] == 0
        assert row["conflicts"] is None  # absent classify section, not 0


class TestSweepTrendRows:
    def test_oldest_first_and_excludes_coverage_runs(self):
        runs = [
            ("k2", {
                "run_kind": "sweep", "run_start": "2026-07-19T11:37:29Z",
                "digest_markdown": FULL_DIGEST,
            }),
            ("k1", {
                "run_kind": "sweep", "run_start": "2026-07-18T08:23:55Z",
                "digest_markdown": FULL_DIGEST,
            }),
            ("k0", {
                "run_kind": "coverage", "run_start": "2026-07-17T07:00:00Z",
                "issues": [],
            }),
        ]
        rows = sweep_trend_rows(runs)
        assert len(rows) == 2  # coverage run excluded
        assert rows[0]["key"] == "k1"  # oldest first
        assert rows[1]["key"] == "k2"


class TestMergeThroughputByPath:
    def test_totals_per_path(self):
        rows = [
            {"scanner_merged": 1, "standing_merged": 2, "reviewed_merged": 6},
            {"scanner_merged": 0, "standing_merged": 3, "reviewed_merged": 1},
        ]
        result = merge_throughput_by_path(rows)
        assert result == {
            "scanner": 1, "standing-exception": 5, "groom-reviewed": 7,
        }

    def test_empty_rows(self):
        assert merge_throughput_by_path([]) == {
            "scanner": 0, "standing-exception": 0, "groom-reviewed": 0,
        }


class TestReviewGateVerdictRows:
    def test_maps_merged_approved_blocked(self):
        rows = [{
            "run_start": datetime(2026, 7, 19, tzinfo=timezone.utc),
            "reviewed_merged": 6, "reviewed_approved_dry_run": 0,
            "reviewed_blocked": 3,
        }]
        out = review_gate_verdict_rows(rows)
        assert out == [{
            "run_start": datetime(2026, 7, 19, tzinfo=timezone.utc),
            "merged": 6, "approved_dry_run": 0, "blocked": 3,
        }]
