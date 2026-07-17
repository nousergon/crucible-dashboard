"""Tests for the Predictor console page + its deep-link slug contract.

The alpha-engine-predictor slim morning-briefing email (config#856 pipeline-
reporting revamp, ``inference/stages/write_output.py``, slug
``PREDICTOR_SLUG = "predictor"``) deep-links to ``…/predictor?date=YYYY-MM-DD``.
So app.py MUST register the page with ``url_path="predictor"`` and the page
MUST honor the ``?date=`` query param. A drift here silently breaks every
emailed link — mirrors the EOD Report / Model Zoo / Analysis page guards.
"""

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

# Pinned slug — must equal alpha-engine-predictor
# inference/stages/write_output.PREDICTOR_SLUG.
EXPECTED_SLUG = "predictor"
REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "7_Predictor.py"


class TestListPredictionsDates:
    def _client(self, keys):
        page = {"Contents": [{"Key": k} for k in keys]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_lists_and_sorts_newest_first_excluding_latest(self):
        keys = [
            "predictor/predictions/2026-07-01.json",
            "predictor/predictions/2026-07-03.json",
            "predictor/predictions/latest.json",       # excluded (sidecar)
            "predictor/predictions/not-a-date.json",    # excluded (not ISO date)
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client",
                             return_value=self._client(keys)):
            assert s3_loader.list_predictions_dates() == [
                "2026-07-03", "2026-07-01",
            ]

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            assert s3_loader.list_predictions_dates() == []


class TestSlugContract:
    def test_app_pins_predictor_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_page_honors_date_query_param(self):
        # The deep-link is useless if the page ignores ?date=; assert the
        # page reads it and writes it back when recognized, like the EOD /
        # Model Zoo pages.
        src = PAGE.read_text()
        assert 'st.query_params.get("date")' in src
        assert 'st.query_params["date"]' in src

    def test_host_predictor_no_longer_double_registers_the_page(self):
        # 7_Predictor.py moved to a standalone pinned page (config#856); it
        # must not also be listed in host_predictor.py's tab set, or the
        # console would render it twice under two different slugs.
        host_src = (REPO_ROOT / "views" / "host_predictor.py").read_text()
        assert '"7_Predictor.py"' not in host_src
