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
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'page("37_Saturday_SF_Watch.py"' in app_src

    def test_page_uses_watch_loaders(self):
        src = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()
        assert "list_saturday_sf_watch_dates" in src
        assert "load_saturday_sf_watch" in src
