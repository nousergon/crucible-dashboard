"""Tests for the PR Pipeline console page + its loaders (config#2709).

Covers ``list_groom_run_keys_since`` (s3_loader), the live open-PR-by-class
census (``pr_merge_loader.classify_open_pr`` / ``load_open_prs_by_class``),
and the nav-registration contract (host_system_health.py must register
``54_PR_Pipeline.py``). Mirrors ``test_backlog_groom_page.py`` /
``test_merged_prs_page.py``: streamlit is mocked (cache_data -> passthrough)
and the page module itself is NOT imported (its module-level Streamlit calls
need a live runtime) — page wiring is asserted against source text instead.
"""

import datetime as _dt
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import pr_merge_loader, s3_loader  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


class TestListGroomRunKeysSince:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_returns_all_keys_not_capped_like_list_groom_run_keys(self):
        # 40 keys across one date — list_groom_run_keys(limit=30) would
        # truncate; list_groom_run_keys_since must not.
        keys = [f"groom/2026-07-19/{i:06d}.json" for i in range(40)]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            assert len(s3_loader.list_groom_run_keys_since(days=14)) == 40

    def test_filters_by_date_window(self):
        today = _dt.date.today()
        in_window = (today - _dt.timedelta(days=1)).isoformat()
        out_of_window = (today - _dt.timedelta(days=20)).isoformat()
        keys = [
            f"groom/{in_window}/sweep-010101.json",
            f"groom/{out_of_window}/sweep-020202.json",
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            result = s3_loader.list_groom_run_keys_since(days=14)
            assert result == [f"groom/{in_window}/sweep-010101.json"]

    def test_excludes_control_plane_and_in_progress_marker(self):
        keys = [
            "groom/_control/completed/94332963e93a.json",
            "groom/in_progress.json",
            f"groom/{_dt.date.today().isoformat()}/sweep-103000.json",
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=self._client(keys)):
            result = s3_loader.list_groom_run_keys_since(days=14)
            assert result == [f"groom/{_dt.date.today().isoformat()}/sweep-103000.json"]

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_groom_run_keys_since() == []


class TestClassifyOpenPr:
    def test_dependabot_wins_even_with_gate_label(self):
        # pr_sweep_classify.py checks dependabot BEFORE gate — a Dependabot
        # PR that picked up a gate label must still land in "dependabot",
        # matching what the live sweep pipeline would actually do with it.
        row = {"author": "dependabot[bot]", "labels": ["gate:ci-red"]}
        assert pr_merge_loader.classify_open_pr(row) == "dependabot"

    def test_gate_label_routes_to_gated(self):
        row = {"author": "cipher813", "labels": ["gate:security-review"]}
        assert pr_merge_loader.classify_open_pr(row) == "gated"

    def test_do_not_groom_routes_to_other(self):
        row = {"author": "cipher813", "labels": ["do-not-groom"]}
        assert pr_merge_loader.classify_open_pr(row) == "other"

    def test_plain_pr_is_groom_ready(self):
        row = {"author": "cipher813", "labels": ["source:groom"]}
        assert pr_merge_loader.classify_open_pr(row) == "groom-ready"

    def test_no_labels_no_author_defaults_groom_ready(self):
        assert pr_merge_loader.classify_open_pr({}) == "groom-ready"


class TestLoadOpenPrsByClass:
    def test_aggregates_single_page(self):
        payload = {
            "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {"author": {"login": "dependabot[bot]"}, "labels": {"nodes": []}},
                    {"author": {"login": "cipher813"},
                     "labels": {"nodes": [{"name": "gate:ci-red"}]}},
                    {"author": {"login": "cipher813"},
                     "labels": {"nodes": [{"name": "source:groom"}]}},
                    {"author": {"login": "cipher813"}, "labels": {"nodes": []}},
                ],
            },
        }
        with patch.object(pr_merge_loader, "_github_graphql", return_value=payload), \
                patch.object(pr_merge_loader, "_github_token", return_value="tok"):
            counts = pr_merge_loader.load_open_prs_by_class()
        assert counts == {"dependabot": 1, "gated": 1, "groom-ready": 2, "other": 0}

    def test_paginates(self):
        page1 = {
            "search": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [{"author": {"login": "cipher813"}, "labels": {"nodes": []}}],
            },
        }
        page2 = {
            "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"author": {"login": "cipher813"}, "labels": {"nodes": []}}],
            },
        }
        with patch.object(pr_merge_loader, "_github_graphql",
                           side_effect=[page1, page2]) as mock_gql, \
                patch.object(pr_merge_loader, "_github_token", return_value="tok"):
            counts = pr_merge_loader.load_open_prs_by_class()
        assert counts["groom-ready"] == 2
        assert mock_gql.call_count == 2

    def test_raises_without_token(self):
        with patch.object(pr_merge_loader, "_github_token", return_value=None):
            try:
                pr_merge_loader.load_open_prs_by_class()
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "No GitHub token" in str(e)


class TestNavRegistration:
    def test_host_registers_pr_pipeline_page(self):
        text = (REPO_ROOT / "views" / "host_system_health.py").read_text()
        assert "54_PR_Pipeline.py" in text
        assert "PR Pipeline" in text

    def test_page_source_uses_expected_loaders(self):
        # Wiring sanity: the view imports the loaders this test suite covers
        # rather than reinventing S3/GraphQL access inline.
        src = (REPO_ROOT / "views" / "54_PR_Pipeline.py").read_text()
        assert "list_groom_run_keys_since" in src
        assert "load_open_prs_by_class" in src
        assert "sweep_trend_rows" in src
        assert "merge_throughput_by_path" in src
        assert "review_gate_verdict_rows" in src


class TestLoadGroomRunIntegration:
    """Sanity check that a real sweep-artifact shape (run_kind='sweep',
    digest_markdown with DONE lines) survives load_groom_run + parses
    cleanly end to end — the actual producer/consumer contract this page
    depends on."""

    def test_sweep_artifact_round_trips(self):
        from loaders.pr_pipeline import sweep_cycle_row

        payload = {
            "schema_version": 9,
            "run_kind": "sweep",
            "run_start": "2026-07-19T11:37:29Z",
            "digest_markdown": (
                "**Still CONFLICTING (needs manual/agent merge):** 0\n\n"
                "**Still CI-RED:** 3\n\n"
                "**Clean + green + ready (no action needed):** 5\n\n"
                "SCANNER_MERGE_SWEEP_DONE evaluated=8 merged=1 "
                "would_merge_if_enabled=0 enabled=True dry_run=False "
                "attribution_failed=0\n"
                "STALENESS_FLUSH_DONE flushed_gated=0 flushed_ready=0 "
                "linkage_violations=0 skipped_recent=0 flush_failed=0 "
                "repos_failed=0 dry_run=False\n"
            ),
        }
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            run = s3_loader.load_groom_run("groom/2026-07-19/sweep-114745.json")
        assert run is not None
        row = sweep_cycle_row("groom/2026-07-19/sweep-114745.json", run)
        assert row["scanner_merged"] == 1
        assert row["conflicts"] == 0
        assert row["ci_red"] == 3
        assert row["clean_ready"] == 5
