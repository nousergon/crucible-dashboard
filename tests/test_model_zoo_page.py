"""Tests for the Model Zoo console page + its deep-link slug contract.

The predictor's weekly Model-Zoo Rotation digest email (crucible-predictor
``training/model_zoo.py``, slug ``model-zoo``) deep-links to
``…/model-zoo?date=YYYY-MM-DD`` — the rotation trading-day key. So app.py MUST
register the page with ``url_path="model-zoo"`` and the page MUST honor the
``?date=`` query param. A drift here silently breaks every emailed link.
"""

import sys
from pathlib import Path

# Mock streamlit before importing anything that touches it.
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

# Pinned slug — must equal the producer slug in crucible-predictor
# training/model_zoo.py (MODEL_ZOO_SLUG).
EXPECTED_SLUG = "model-zoo"
REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "35_Model_Zoo.py"


class TestSlugContract:
    def test_app_pins_model_zoo_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_page_honors_date_query_param(self):
        # The deep-link is useless if the page ignores ?date=; assert the page
        # reads it and writes it back (selectbox sync), like the EOD page.
        src = PAGE.read_text()
        assert 'st.query_params.get("date")' in src
        assert 'st.query_params["date"]' in src
