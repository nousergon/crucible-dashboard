"""Tests for the Saturday SF Watch console page + its loaders.

Covers the watch-log loaders (``load_saturday_sf_watch`` /
``list_saturday_sf_watch_dates``) and the nav-registration contract (app.py
must register ``37_Saturday_SF_Watch.py`` under System & Ops). The page reads
the failure-driven watch-log written by the alpha-engine-data
``saturday-sf-watch-dispatcher`` Lambda (config#1227).

Mirrors test_eod_report_page.py: streamlit is mocked (cache_data → passthrough)
and the page module itself is NOT imported (its module-level Streamlit calls
need a live runtime) — page wiring is asserted against app.py source text.
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


class TestLoadSaturdaySfWatch:
    def test_returns_dict_on_valid_json(self):
        payload = {"schema_version": 1, "run_date": "2026-06-20", "events": []}
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            assert s3_loader.load_saturday_sf_watch("2026-06-20") == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_saturday_sf_watch("2026-06-20") is None

    def test_returns_none_on_non_dict_json(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=b"[1, 2, 3]"):
            assert s3_loader.load_saturday_sf_watch("2026-06-20") is None


class TestListSaturdaySfWatchDates:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_and_sorts_newest_first(self):
        keys = [
            "consolidated/saturday_sf_watch/2026-06-13.json",
            "consolidated/saturday_sf_watch/2026-06-20.json",
            "consolidated/saturday_sf_watch/latest.json",   # ignored (not ISO date)
            "consolidated/saturday_sf_watch/2026-06-20.txt",  # ignored (not .json)
            "consolidated/saturday_sf_watch/",                # ignored (prefix marker)
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client(keys)):
            assert s3_loader.list_saturday_sf_watch_dates() == [
                "2026-06-20", "2026-06-13",
            ]

    def test_empty_when_no_failures(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client([])):
            assert s3_loader.list_saturday_sf_watch_dates() == []

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_saturday_sf_watch_dates() == []


class TestNavRegistration:
    def test_page_file_exists(self):
        assert (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").exists()

    def test_app_registers_page(self):
        # Hosted under the System Health front page (lazy view-host) post-IA-reorg
        # rather than registered directly in app.py.
        host_src = (REPO_ROOT / "views" / "host_system_health.py").read_text()
        assert '"37_Saturday_SF_Watch.py"' in host_src
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'page("host_system_health.py"' in app_src

    def test_page_uses_watch_loaders(self):
        src = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()
        assert "list_saturday_sf_watch_dates" in src
        assert "load_saturday_sf_watch" in src


# ── Saturday Integrity gate (GO/NO-GO banner — config#1244) ──────────────────


class TestLoadSaturdayIntegrity:
    def test_returns_dict_on_valid_json(self):
        payload = {"status": "GO", "checked_at": "2026-06-20T12:00:00Z"}
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            assert s3_loader.load_saturday_integrity("2026-06-20") == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_saturday_integrity("2026-06-20") is None

    def test_returns_none_on_non_dict_json(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=b"[1, 2, 3]"):
            assert s3_loader.load_saturday_integrity("2026-06-20") is None


class TestListSaturdayIntegrityDates:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_and_sorts_newest_first(self):
        keys = [
            "consolidated/saturday_integrity/2026-06-13.json",
            "consolidated/saturday_integrity/2026-06-20.json",
            "consolidated/saturday_integrity/latest.json",   # ignored (not ISO date)
            "consolidated/saturday_integrity/2026-06-20.txt",  # ignored (not .json)
            "consolidated/saturday_integrity/",                # ignored (prefix marker)
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client(keys)):
            assert s3_loader.list_saturday_integrity_dates() == [
                "2026-06-20", "2026-06-13",
            ]

    def test_empty_when_no_marker(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client([])):
            assert s3_loader.list_saturday_integrity_dates() == []

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_saturday_integrity_dates() == []


class TestPageIntegrationFields:
    """The page must wire the GO/NO-GO banner loaders + the agent enrichment
    fields (pr_urls / diagnosis / recommended_command) — config#1244."""

    def test_page_uses_integrity_loaders(self):
        src = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()
        assert "list_saturday_integrity_dates" in src
        assert "load_saturday_integrity" in src

    def test_page_surfaces_agent_enrichment_fields(self):
        src = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()
        assert "pr_urls" in src
        assert "diagnosis" in src
        assert "recommended_command" in src


# ── Fleet CI Watch (main-branch CI/deploy red events — config#1593/#1596) ────


class TestLoadCiWatch:
    def test_returns_dict_on_valid_json(self):
        payload = {"schema_version": 2, "events": []}
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            assert s3_loader.load_ci_watch("2026-07-02") == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_ci_watch("2026-07-02") is None

    def test_returns_none_on_non_dict_json(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=b"[1, 2, 3]"):
            assert s3_loader.load_ci_watch("2026-07-02") is None


class TestListCiWatchDates:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_and_sorts_newest_first(self):
        keys = [
            "consolidated/ci_watch/2026-07-01.json",
            "consolidated/ci_watch/2026-07-02.json",
            "consolidated/ci_watch/latest.json",   # ignored (not ISO date)
            "consolidated/ci_watch/2026-07-02.txt",  # ignored (not .json)
            "consolidated/ci_watch/",                # ignored (prefix marker)
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client(keys)):
            assert s3_loader.list_ci_watch_dates() == ["2026-07-02", "2026-07-01"]

    def test_empty_when_no_events(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client([])):
            assert s3_loader.list_ci_watch_dates() == []

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_ci_watch_dates() == []


class TestPageWiresCiWatch:
    def test_page_uses_ci_watch_loaders(self):
        src = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()
        assert "list_ci_watch_dates" in src
        assert "load_ci_watch" in src

    def test_page_surfaces_ci_watch_schema_fields(self):
        src = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()
        for field in ("repo", "run_id", "run_url", "sha", "workflow",
                      "agent_attempt", "lane", "action", "pr_urls",
                      "diagnosis", "rerun_conclusion", "followup_issues"):
            assert field in src, f"missing {field}"
