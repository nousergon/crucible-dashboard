"""Tests for groom run ↔ usage join + efficiency ratios."""

from datetime import datetime, timezone

import pytest

from loaders.groom_efficiency import (
    compute_efficiency,
    match_usage_for_run,
    model_scorecard_rows,
    parse_usage_key_timestamp,
    short_model_name,
    usage_record_from_doc,
)


def _run(*, start="2026-07-06T07:02:42Z", elapsed=49, engaged=70, total=76,
         issue_filter="low-only", floor_fail=False):
    return {
        "run_start": start,
        "elapsed_min": elapsed,
        "engaged": engaged,
        "total_issues": total,
        "issue_filter": issue_filter,
        "floor_fail": floor_fail,
    }


def _issues(closed=3, prs=0, commented=67, untouched=6):
    out = []
    for _ in range(closed):
        out.append({"disposition": "closed"})
    for _ in range(prs):
        out.append({"disposition": "pr_opened"})
    for _ in range(commented):
        out.append({"disposition": "commented"})
    for _ in range(untouched):
        out.append({"disposition": "untouched"})
    return out


def test_parse_usage_key_timestamp():
    key = "claude_code_usage/groom/2026-07-06/20260706T075115Z-i-0232.json"
    ts = parse_usage_key_timestamp(key)
    assert ts == datetime(2026, 7, 6, 7, 51, 15, tzinfo=timezone.utc)


def test_match_usage_by_nearest_end_time():
    run_key = "groom/2026-07-06/94332963e93a.json"
    run = _run()
    usage = [
        usage_record_from_doc(
            "claude_code_usage/groom/2026-07-06/20260706T075115Z-i-0232.json",
            {"day_total": {"wet": 1_800_000, "total": 100, "cache_read_input_tokens": 97}},
        ),
        usage_record_from_doc(
            "claude_code_usage/groom/2026-07-06/20260706T120000Z-i-other.json",
            {"day_total": {"wet": 5_000_000, "total": 100, "cache_read_input_tokens": 90}},
        ),
    ]
    matched = match_usage_for_run(run_key, run, usage)
    assert matched is not None
    assert "075115Z" in matched["key"]


def test_match_usage_direct_run_id():
    run_key = "groom/2026-07-01/28381575147-1.json"
    run = _run(start="2026-07-01T19:00:00Z", elapsed=60)
    key = "claude_code_usage/groom/2026-07-01/28381575147-1.json"
    usage = [usage_record_from_doc(key, {"day_total": {"wet": 7_400_000, "total": 1, "cache_read_input_tokens": 1}})]
    matched = match_usage_for_run(run_key, run, usage)
    assert matched["key"] == key


def test_compute_efficiency_ratios():
    run = _run(engaged=70, issue_filter="low-only")
    issues = _issues(closed=3, commented=61, untouched=6)
    usage = {"wet": 1_800_000, "cache_read_pct": 97.0, "key": "k"}
    eff = compute_efficiency(run, issues, usage)
    assert eff["wet_per_engaged"] == pytest.approx(1_800_000 / 70)
    assert eff["throughput"] == pytest.approx(70 / 49)
    assert eff["hard_rate"] == pytest.approx(3 / 70)
    assert eff["usage_matched"] is True


def test_compute_efficiency_alerts_high_untouched():
    run = _run(engaged=10, total=100, issue_filter="high-only")
    issues = _issues(closed=0, commented=5, untouched=90)
    eff = compute_efficiency(run, issues, None)
    assert any("untouched" in a for a in eff["alerts"])


def test_usage_record_skips_manual_reset():
    key = "claude_code_usage/groom/2026-07-04/zz-manual-reset-260704.json"
    assert usage_record_from_doc(key, {"day_total": {"wet": -1}}) is None


def test_compute_efficiency_prefers_artifact_run_wet_over_usage_join():
    # config#1894: a schema_version>=5 artifact's own run_wet (exact, driver-
    # measured) beats the heuristic date+end-time usage join.
    from loaders.groom_efficiency import compute_efficiency
    run = {"engaged": 10, "total_issues": 10, "elapsed_min": 30,
           "soft_limit_min": 60, "issue_filter": "low-only",
           "schema_version": 5, "run_wet": 2_000_000.0}
    usage = {"key": "u1", "wet": 9_999_999.0, "cache_read_pct": 96.0}
    eff = compute_efficiency(run, [], usage)
    assert eff["wet"] == 2_000_000.0
    assert eff["wet_per_engaged"] == 200_000.0
    assert eff["cache_read_pct"] == 96.0  # still from the usage record


def test_compute_efficiency_falls_back_to_usage_join_pre_schema5():
    from loaders.groom_efficiency import compute_efficiency
    run = {"engaged": 4, "total_issues": 4, "elapsed_min": 10,
           "soft_limit_min": 60, "issue_filter": "low-only", "schema_version": 4}
    usage = {"key": "u1", "wet": 400_000.0, "cache_read_pct": 95.0}
    eff = compute_efficiency(run, [], usage)
    assert eff["wet"] == 400_000.0
    assert eff["wet_per_engaged"] == 100_000.0


# ── config-I2746: Model column + Model scorecard ────────────────────────────


class TestShortModelName:
    def test_strips_claude_prefix_opus(self):
        assert short_model_name("claude-opus-4-8") == "opus-4-8"

    def test_strips_claude_prefix_sonnet(self):
        assert short_model_name("claude-sonnet-5") == "sonnet-5"

    def test_passthrough_when_no_claude_prefix(self):
        assert short_model_name("gpt-4o") == "gpt-4o"

    def test_dash_on_missing(self):
        assert short_model_name(None) == "—"
        assert short_model_name("") == "—"


class TestModelScorecardRows:
    def _run(self, *, model="claude-opus-4-8", issue_filter="high-only",
              issues=None, elapsed_min=30, processed=None, total_issues=None,
              stop_reason=None):
        issues = issues if issues is not None else []
        return {
            "model": model,
            "issue_filter": issue_filter,
            "issues": issues,
            "elapsed_min": elapsed_min,
            "processed": processed if processed is not None else len(issues),
            "total_issues": total_issues if total_issues is not None else len(issues),
            "stop_reason": stop_reason,
        }

    def _issues(self, closed=0, prs=0, commented=0, untouched=0):
        out = []
        out += [{"disposition": "closed"}] * closed
        out += [{"disposition": "pr_opened"}] * prs
        out += [{"disposition": "commented"}] * commented
        out += [{"disposition": "untouched"}] * untouched
        return out

    def test_groups_by_model_and_tier_and_aggregates(self):
        run1 = self._run(issues=self._issues(closed=1, commented=1),
                          elapsed_min=30)
        eff1 = {"engaged": 2, "wet": 1_000_000.0}
        run2 = self._run(issues=self._issues(prs=1, commented=2),
                          elapsed_min=45)
        eff2 = {"engaged": 3, "wet": 2_000_000.0}
        rows = model_scorecard_rows([(run1, eff1), (run2, eff2)])
        assert len(rows) == 1
        row = rows[0]
        assert row["Model"] == "opus-4-8"
        assert row["Tier"] == "high-only"
        assert row["Runs"] == 2
        assert row["Touches"] == 5
        assert row["Hard-outcome rate"] == "40%"   # 2 hard / 5 touches
        assert row["Comment-only %"] == "60%"       # 3 commented / 5 touches
        assert row["Untouched %"] == "0%"
        assert row["Total WET"] == "3.0M"
        assert row["WET/hard"] == "1500K"           # 3.0M / 2 hard, in K
        assert row["Crashes"] == 0
        assert row["Min/issue"] == "15.0"           # 75 elapsed / 5 processed

    def test_excludes_degenerate_zero_engaged_runs(self):
        run = self._run(issues=self._issues(commented=1))
        eff = {"engaged": 0, "wet": 500_000.0}
        assert model_scorecard_rows([(run, eff)]) == []

    def test_renders_dash_when_wet_unmeasured_not_crash(self):
        # Gotcha: pre-2026-07-07 artifacts (or an unmatched usage join) carry
        # no run_wet — must render "—", never crash on the None.
        run = self._run(model="claude-sonnet-5", issue_filter="mid-only",
                         issues=self._issues(closed=1))
        eff = {"engaged": 1, "wet": None}
        rows = model_scorecard_rows([(run, eff)])
        assert rows[0]["Total WET"] == "—"
        assert rows[0]["WET/hard"] == "—"

    def test_counts_crash_abort_stop_reason(self):
        run = self._run(issues=[], stop_reason="crash: instance reclaimed mid-run")
        eff = {"engaged": 1, "wet": None}
        rows = model_scorecard_rows([(run, eff)])
        assert rows[0]["Crashes"] == 1

    def test_untouched_pct_uses_queued_not_touches(self):
        run = self._run(issues=self._issues(closed=1, untouched=1),
                         total_issues=2)
        eff = {"engaged": 1, "wet": None}
        rows = model_scorecard_rows([(run, eff)])
        # 1 untouched / 2 queued, NOT 1 untouched / 1 touch
        assert rows[0]["Untouched %"] == "50%"

    def test_sorted_by_model_then_tier(self):
        run_sonnet = self._run(model="claude-sonnet-5", issue_filter="high-only",
                                issues=self._issues(closed=1))
        run_opus = self._run(model="claude-opus-4-8", issue_filter="high-only",
                              issues=self._issues(closed=1))
        eff = {"engaged": 1, "wet": None}
        rows = model_scorecard_rows([(run_sonnet, eff), (run_opus, eff)])
        assert [r["Model"] for r in rows] == ["opus-4-8", "sonnet-5"]
