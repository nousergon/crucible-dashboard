"""Tests for the EOD Report console page + its loaders.

Covers the structured-artifact loaders (``load_eod_report`` /
``list_eod_report_dates``) and the pinned deep-link slug contract: the EOD
email (alpha-engine executor ``eod_emailer.EOD_REPORT_SLUG = "eod-report"``)
links to ``…/eod-report?date=…``, so app.py MUST register the page with
``url_path="eod-report"``. A drift here silently breaks every emailed link.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock streamlit before importing the loader (cache_data → passthrough).
mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import s3_loader  # noqa: E402

# Pinned slug — must equal alpha-engine executor eod_emailer.EOD_REPORT_SLUG.
EXPECTED_SLUG = "eod-report"
REPO_ROOT = Path(__file__).parent.parent


class TestLoadEodReport:
    def test_returns_dict_on_valid_json(self):
        payload = {"schema_version": "1.0", "run_date": "2026-06-22", "summary": {}}
        with patch.object(s3_loader, "_trades_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            assert s3_loader.load_eod_report("2026-06-22") == payload

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_trades_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_eod_report("2026-06-22") is None

    def test_returns_none_on_non_dict_json(self):
        with patch.object(s3_loader, "_trades_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=b"[1, 2, 3]"):
            assert s3_loader.load_eod_report("2026-06-22") is None


class TestListEodReportDates:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_and_sorts_newest_first(self):
        keys = [
            "consolidated/2026-06-18/eod_report.json",
            "consolidated/2026-06-22/eod_report.json",
            "consolidated/2026-06-18/eod.html",        # ignored (wrong basename)
            "consolidated/2026-06-22/morning.md",       # ignored
            "consolidated/not-a-date/eod_report.json",  # ignored (not ISO date)
        ]
        with patch.object(s3_loader, "_trades_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client(keys)):
            assert s3_loader.list_eod_report_dates() == ["2026-06-22", "2026-06-18"]

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_trades_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_eod_report_dates() == []


class TestSlugContract:
    def test_app_pins_eod_report_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_page_file_exists(self):
        # The EOD report is now the as-of view of the canonical Performance page
        # (merged Portfolio + EOD Report + Attribution Heatmaps); it owns the
        # eod-report slug. The old standalone EOD Report page is retired.
        assert (REPO_ROOT / "views" / "1_Performance.py").exists()
        assert not (REPO_ROOT / "views" / "19_EOD_Report.py").exists()

    def test_old_archive_page_removed(self):
        assert not (REPO_ROOT / "views" / "19_EOD_Reconcile_Archive.py").exists()
