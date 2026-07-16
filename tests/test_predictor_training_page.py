"""Tests for the Predictor Training console page + its deep-link slug contract.

The predictor's weekly training-summary email (crucible-predictor
``training/train_handler.py``, slug ``TRAINING_PAGE_SLUG = "predictor-training"``)
was slimmed to a headline + console deep-link under config#856; it deep-links to
``…/predictor-training?date=YYYY-MM-DD`` — the training cycle's trading-day key.
So app.py MUST register the page with ``url_path="predictor-training"`` and the
page MUST honor the ``?date=`` query param. A drift here silently breaks every
emailed training link (lands on some run, not the cycle the email describes).
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
# training/train_handler.py (TRAINING_PAGE_SLUG).
EXPECTED_SLUG = "predictor-training"
REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "36_Predictor_Training.py"


class TestSlugContract:
    def test_app_pins_predictor_training_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_page_honors_date_query_param(self):
        # The deep-link is useless if the page ignores ?date=; assert the page
        # reads it and writes it back (selectbox sync), like the EOD / Model Zoo
        # pages.
        src = PAGE.read_text()
        assert 'st.query_params.get("date")' in src
        assert 'st.query_params["date"]' in src


class TestLoaderContract:
    def test_loaders_exist(self):
        # The page renders the dated training_summary_{date}.json artifact via
        # these two loaders; pin their presence so a rename can't silently break
        # the page.
        from loaders import s3_loader

        assert hasattr(s3_loader, "list_predictor_training_dates")
        assert hasattr(s3_loader, "load_predictor_training_summary")
