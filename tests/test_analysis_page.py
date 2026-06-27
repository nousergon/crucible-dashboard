"""Tests for the Analysis (backtester+eval) console page deep-link contract.

The weekly backtester+evaluation digest email (alpha-engine-backtester
``emailer.ANALYSIS_SLUG = "analysis"``) deep-links to
``…/analysis?date=YYYY-MM-DD`` — the backtest run_date. So app.py MUST register
the page with ``url_path="analysis"`` and the page MUST honor ``?date=``.
A drift here silently breaks the emailed link.
"""

from pathlib import Path

# Pinned slug — must equal the producer slug in alpha-engine-backtester
# emailer.ANALYSIS_SLUG.
EXPECTED_SLUG = "analysis"
REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "3_Analysis.py"


class TestSlugContract:
    def test_app_pins_analysis_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_page_honors_date_query_param(self):
        src = PAGE.read_text()
        assert 'st.query_params.get("date")' in src
        assert 'st.query_params["date"]' in src
