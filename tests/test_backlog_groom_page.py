"""Tests for the Backlog Groom console page + its loaders.

Covers the run-artifact loaders (``load_groom_run`` / ``list_groom_run_keys``)
and the nav-registration contract (host_system_health.py must register
``42_Backlog_Groom.py`` under System & Ops). The page reads the per-run
artifact written by ``alpha-engine-config``'s ``groom_driver.py::write_run_artifact``
(config#1495, #1512).

Mirrors test_saturday_sf_watch_page.py: streamlit is mocked (cache_data ->
passthrough) and the page module itself is NOT imported (its module-level
Streamlit calls need a live runtime) — page wiring is asserted against source
text instead.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import s3_loader  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


class TestLoadGroomRun:
    def test_returns_dict_on_valid_json(self):
        payload = {"schema_version": 1, "run_start": "2026-07-01T15:42:17Z", "issues": []}
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            assert s3_loader.load_groom_run("groom/2026-07-01/153042.json") == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_groom_run("groom/2026-07-01/153042.json") is None

    def test_returns_none_on_non_dict_json(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=b"[1, 2, 3]"):
            assert s3_loader.load_groom_run("groom/2026-07-01/153042.json") is None


class TestListGroomRunKeys:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_and_sorts_newest_first_across_multiple_runs_per_date(self):
        # Unlike Saturday SF Watch (one file per date), groom runs 3x/day —
        # multiple artifacts land under the SAME date prefix.
        keys = [
            "groom/2026-07-01/070012.json",
            "groom/2026-07-01/153042.json",
            "groom/2026-06-30/230511.json",
            "groom/2026-07-01/notes.txt",  # ignored (not .json)
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            assert s3_loader.list_groom_run_keys() == [
                "groom/2026-07-01/153042.json",
                "groom/2026-07-01/070012.json",
                "groom/2026-06-30/230511.json",
            ]

    def test_excludes_control_plane_and_in_progress_marker(self):
        # groom/ also hosts the dispatcher control plane (groom/_control/*,
        # nousergon-data#658) and the in-progress marker — "_" sorts AFTER
        # digits, so unfiltered these displace every real run at the head
        # of the reverse sort (bit Fleet Status + this page 2026-07-06).
        keys = [
            "groom/_control/completed/94332963e93a.json",
            "groom/in_progress.json",
            "groom/2026-07-05/103000.json",
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            assert s3_loader.list_groom_run_keys() == [
                "groom/2026-07-05/103000.json",
            ]

    def test_respects_limit(self):
        keys = [f"groom/2026-07-01/{i:06d}.json" for i in range(5)]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            assert len(s3_loader.list_groom_run_keys(limit=2)) == 2

    def test_empty_when_no_artifacts_yet(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client([])):
            assert s3_loader.list_groom_run_keys() == []

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_groom_run_keys() == []


class TestListGroomDecisionKeys:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def _fixed_today(self, iso_date):
        import datetime as dt

        class _FixedDate(dt.date):
            @classmethod
            def today(cls):
                return dt.date(*map(int, iso_date.split("-")))

        return _FixedDate

    def test_lists_within_window_newest_first(self):
        keys = [
            "groom/decisions/2026-07-08/trigger-0100.json",
            "groom/decisions/2026-07-07/trigger-1900.json",
            "groom/decisions/2026-07-07/trigger-0700.json",
            "groom/decisions/2026-07-01/trigger-0100.json",  # outside 3-day window
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)), \
                patch("datetime.date", self._fixed_today("2026-07-08")):
            result = s3_loader.list_groom_decision_keys(days=3)
        assert result == [
            "groom/decisions/2026-07-08/trigger-0100.json",
            "groom/decisions/2026-07-07/trigger-1900.json",
            "groom/decisions/2026-07-07/trigger-0700.json",
        ]

    def test_empty_when_no_records_yet(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client([])):
            assert s3_loader.list_groom_decision_keys() == []

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_groom_decision_keys() == []


class TestLoadGroomDecision:
    def test_returns_dict_on_valid_json(self):
        payload = {"schema_version": 2, "decisions": [], "decided_at": "x"}
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            key = "groom/decisions/2026-07-08/trigger-0100.json"
            assert s3_loader.load_groom_decision(key) == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            key = "groom/decisions/2026-07-08/trigger-0100.json"
            assert s3_loader.load_groom_decision(key) is None


class TestNormalizeGroomDecisionRecord:
    def test_schema_v2_decisions_list_passthrough(self):
        raw = {
            "schema_version": 2,
            "decisions": [
                {"launch": True, "tiers": ["high"], "issue_filter": "high-only",
                 "model": "claude-opus-4-8", "reason": "29 actionable"},
            ],
        }
        assert s3_loader.normalize_groom_decision_record(raw) == raw["decisions"]

    def test_schema_v2_empty_decisions_is_full_skip(self):
        raw = {"schema_version": 2, "decisions": []}
        assert s3_loader.normalize_groom_decision_record(raw) == []

    def test_schema_v1_singular_fields_wrapped(self):
        raw = {
            "schema_version": 1, "slot_tier": "mid", "launch": False,
            "tiers": [], "issue_filter": "", "model": "",
            "reason": "8 actionable, below floor 10",
        }
        result = s3_loader.normalize_groom_decision_record(raw)
        assert len(result) == 1
        assert result[0]["launch"] is False
        assert result[0]["reason"] == "8 actionable, below floor 10"
        assert result[0]["slot_tier"] == "mid"

    def test_malformed_record_returns_empty_list(self):
        assert s3_loader.normalize_groom_decision_record({}) == []
        assert s3_loader.normalize_groom_decision_record(None) == []  # type: ignore[arg-type]


class TestKnownSlotsFromRecords:
    def test_extracts_distinct_slot_names(self):
        keys = [
            "groom/decisions/2026-07-08/trigger-0100.json",
            "groom/decisions/2026-07-07/trigger-0100.json",
            "groom/decisions/2026-07-07/trigger-1900.json",
        ]
        assert s3_loader.known_slots_from_records(keys) == [
            "trigger-0100", "trigger-1900",
        ]

    def test_empty_when_no_keys(self):
        assert s3_loader.known_slots_from_records([]) == []


class TestNavRegistration:
    def test_page_file_exists(self):
        assert (REPO_ROOT / "views" / "42_Backlog_Groom.py").exists()

    def test_host_registers_page(self):
        host_src = (REPO_ROOT / "views" / "host_system_health.py").read_text()
        assert '"42_Backlog_Groom.py"' in host_src
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'page("host_system_health.py"' in app_src

    def test_page_uses_groom_run_loaders(self):
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "list_groom_run_keys" in src
        assert "load_groom_run" in src

    def test_page_surfaces_per_issue_disposition_fields(self):
        # The whole point of the page: verifiable per-issue disposition, not a
        # self-report — pin that it actually renders the disposition/detail
        # fields the artifact carries.
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "disposition" in src
        assert "detail" in src
        assert "other_closed" in src
        assert "other_prs" in src

    def test_page_surfaces_budget_vs_consumed_fields(self):
        # config#1569: soft_limit_min/elapsed_min/engaged/floor were added to the
        # artifact schema (schema_version 2, alpha-engine-config PR #1570) so the
        # console can answer "why didn't this run use its full soft budget"
        # without opening the linked GitHub groom-digest issue.
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "soft_limit_min" in src
        assert "elapsed_min" in src
        assert "schema_version" in src
        assert "engaged" in src

    def test_page_surfaces_run_digest_and_history(self):
        # schema_version 3 (alpha-engine-config, 2026-07-02): the finalized
        # groom-digest is embedded in the run artifact so the console shows
        # (a) a per-run "Run history" summary table and (b) the digest
        # narrative itself — without a GitHub API dependency (the dashboard
        # is a pure S3 reader by contract). Pre-v3 artifacts must degrade to
        # a pointer at the GitHub groom-digest issues, not error.
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "Run history" in src
        assert "digest_markdown" in src
        assert "digest_title" in src
        assert "digest_issue" in src
        assert "predates digest embedding" in src  # graceful pre-v3 fallback

    def test_page_surfaces_token_efficiency_metrics(self):
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "Token efficiency" in src
        assert "list_groom_usage_records" in src
        assert "compute_efficiency" in src
        assert "WET/eng" in src or "wet_per_engaged" in src

    def test_page_renders_slot_decisions_strip_above_run_history(self):
        # config#1935: the strip must render ABOVE "Run history" and must
        # use the decision loaders (never re-derive decision state from the
        # post-hoc run artifacts).
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "Slot decisions" in src
        assert "list_groom_decision_keys" in src
        assert "load_groom_decision" in src
        assert "normalize_groom_decision_record" in src
        strip_pos = src.index("Slot decisions")
        history_pos = src.index("Run history")
        assert strip_pos < history_pos

    def test_page_renders_slot_decisions_strip_before_early_stop(self):
        # The page st.stop()s early when there are zero groom RUN artifacts
        # (list_groom_run_keys() == []) — the decisions strip must render
        # before that early exit, or it silently vanishes on a clean/cold
        # backlog even though decision records are a separate, always-
        # present-or-loudly-missing source (config#1935 step 6).
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        strip_call_pos = src.index("_render_slot_decisions_strip(_decision_records")
        stop_pos = src.index("st.stop()")
        assert strip_call_pos < stop_pos

    def test_slot_decisions_strip_never_silently_blanks_on_missing_record(self):
        # The whole point of the feature (config#1935): a gap must render a
        # visible warning glyph, not nothing.
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "no decision record" in src
        assert "⚠️" in src

    def test_slot_decisions_strip_handles_zero_records_defensively(self):
        # nousergon-data-PR684 may not have bootstrapped its first record
        # yet at merge time — the strip must degrade to an explicit notice,
        # not crash or render blank.
        src = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()
        assert "cold start" in src.lower() or "cold-start" in src.lower()


class TestListGroomAuditKeys:
    """groom/audit/{date}.json — weekly disposition-quality audit
    (config#2153), surfaced on the page as of the 2026-07-14 redesign."""

    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_newest_first_and_filters_non_audit_keys(self):
        keys = [
            "groom/audit/2026-07-03.json",
            "groom/audit/2026-07-10.json",
            "groom/audit/notes.txt",          # ignored (not date.json)
            "groom/audit/2026-07-10.json.bak",  # ignored (bad shape)
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            assert s3_loader.list_groom_audit_keys() == [
                "groom/audit/2026-07-10.json",
                "groom/audit/2026-07-03.json",
            ]

    def test_respects_limit(self):
        keys = [f"groom/audit/2026-07-{d:02d}.json" for d in range(1, 12)]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            assert len(s3_loader.list_groom_audit_keys(limit=3)) == 3

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client), \
                patch.object(s3_loader, "_record_s3_error"):
            assert s3_loader.list_groom_audit_keys() == []


class TestLoadGroomAudit:
    def test_returns_dict_on_valid_json(self):
        payload = {"schema_version": 1, "date": "2026-07-10",
                   "pass_count": 9, "fail_count": 1, "error_count": 0}
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            assert s3_loader.load_groom_audit("groom/audit/2026-07-10.json") == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_groom_audit("groom/audit/2026-07-10.json") is None


class TestRedesignSurfaces:
    """2026-07-14 readability redesign: decision TABLE (not chips), a
    trailing-window health roll-up, cross-run trends, and the
    disposition-quality audit — pinned the same source-text way the rest
    of this file pins page wiring."""

    def _src(self):
        return (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()

    def test_decisions_render_as_table_with_backlog_counts(self):
        # The dispatcher's counts {low,mid,high} — the single most
        # informative field in the decision record — must be rendered,
        # via the pure decision_table_rows transform.
        src = self._src()
        assert "decision_table_rows" in src
        assert "Actionable low-complexity issues at decision time" in src

    def test_health_rollup_present(self):
        src = self._src()
        assert "Groom health" in src
        assert "window_kpis" in src
        assert "Floor breaches" in src
        assert "Undispositioned" in src

    def test_trends_present(self):
        src = self._src()
        assert "Trends" in src
        assert "demand_trend_rows" in src
        assert "runs_trend_rows" in src
        assert "scatter_chart" in src
        assert "line_chart" in src

    def test_audit_surface_present(self):
        src = self._src()
        assert "list_groom_audit_keys" in src
        assert "load_groom_audit" in src
        assert "Disposition-quality audit" in src

    def test_v9_queue_shape_fields_surfaced(self):
        src = self._src()
        for field in ("undispositioned", "dropped_at_cap", "gated_excluded",
                      "max_turns_chunks", "fresh_skipped"):
            assert field in src, field

    def test_tier_colors_fixed_never_cycled(self):
        # Chart series colors are assigned per tier in fixed order —
        # filtering must not repaint survivors.
        src = self._src()
        assert "TIER_COLOR" in src
        assert "TIER_ORDER" in src


class TestModelColumnAndScorecard:
    """config-I2746: since the 2026-07-13 high-tier cutover (config#2409)
    tier no longer implies model — the console must surface model per run
    (Run history) and per-(model, tier) aggregate performance (Model
    scorecard), both derived from already-loaded run/eff data (no new S3
    reads)."""

    def _src(self):
        return (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()

    def test_run_history_has_model_column(self):
        src = self._src()
        assert '"Model": short_model_name(run.get("model"))' in src

    def test_model_scorecard_section_present_below_run_history(self):
        src = self._src()
        assert "Model scorecard" in src
        history_pos = src.index("Run history")
        scorecard_pos = src.index("Model scorecard")
        assert history_pos < scorecard_pos

    def test_model_scorecard_reuses_loaded_runs_no_new_s3_reads(self):
        src = self._src()
        assert "model_scorecard_rows(" in src
        # Must slice the already-loaded runs (loaded_runs), not re-list/re-load.
        assert "loaded_runs[:_HISTORY_N]" in src

    def test_model_scorecard_caption_notes_queue_composition_confound(self):
        src = self._src()
        caption_section = src[src.index("Model scorecard"):]
        assert "confound" in caption_section.lower()
        assert "config-I2730" in caption_section

    def test_page_imports_scorecard_helpers_from_groom_efficiency(self):
        src = self._src()
        assert "model_scorecard_rows" in src
        assert "short_model_name" in src
