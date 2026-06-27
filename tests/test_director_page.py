"""Tests for the Director console page deep-link contract.

The Director weekly-plan digest email (alpha-engine-evaluator
``director/emailer.DIRECTOR_SLUG = "director"``) deep-links to
``…/director?date=YYYY-MM-DD`` — the run trading-day key. So app.py MUST
register the page with ``url_path="director"`` and the page MUST honor
``?date=``. A drift here silently breaks the emailed link.
"""

from pathlib import Path

# Pinned slug — must equal the producer slug in alpha-engine-evaluator
# director/emailer.DIRECTOR_SLUG.
EXPECTED_SLUG = "director"
REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "Director_Plan.py"


class TestSlugContract:
    def test_app_pins_director_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_page_honors_date_query_param(self):
        src = PAGE.read_text()
        assert 'st.query_params.get("date")' in src
        assert 'st.query_params["date"]' in src
