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
