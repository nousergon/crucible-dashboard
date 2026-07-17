"""Wiring guards for console-IA phase 2b (alpha-engine-config#1988).

Universe 4-in-1, Agent Reviews shared cycle, Feedback Loop absorbed into
Analysis Self-Tuning, Daily News demoted to a Signals-host tab. Source-text
assertions in the repo's usual style.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VIEWS = REPO_ROOT / "views"


class TestUniverseConsolidation:
    def test_universe_host_tabs(self):
        src = (VIEWS / "host_universe_scanner.py").read_text()
        for pair in (
            '("Universe Board", "39_Universe_Board.py")',
            '("Funnel", "34_Scanner.py")',
            '("Trends", "40_Attractiveness_Trends.py")',
            '("Focus Audit", "5_Focus_List.py")',
        ):
            assert pair in src, pair

    def test_focus_list_left_signals_host(self):
        src = (VIEWS / "host_research_signals.py").read_text()
        assert "5_Focus_List.py" not in src


class TestAgentReviewsSharedCycle:
    def test_all_three_tabs_share_the_cycle_key(self):
        for f in ("29_Decision_Review.py", "31_CIO_Review.py", "33_Sector_Team_Review.py"):
            src = (VIEWS / f).read_text()
            assert 'key="agent_reviews_cycle"' in src, f

    def test_population_flow_moved_to_cio_review(self):
        assert "Population Flow & New Entrants" in (VIEWS / "31_CIO_Review.py").read_text()
        assert "compute_entrant_flow" not in (VIEWS / "2_Signals_and_Research.py").read_text()


class TestFeedbackLoopAbsorbed:
    def test_page_deleted(self):
        assert not (VIEWS / "12_Feedback_Loop.py").exists()

    def test_eval_quality_keeps_registry_deep_link_target(self):
        # nousergon-lib v0.115.0+ deep-links the plain "evaluator" slug
        # (host_eval_backtester?tab=Eval+Quality collapsed away — config#2557,
        # 8_Eval_Quality.py registered directly at url_path="evaluator";
        # see tests/test_registry_page_targets.py for the live-resolution
        # guard against the installed lib registry).
        assert not (VIEWS / "host_eval_backtester.py").exists()
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'url_path="evaluator"' in app_src
        src = (VIEWS / "8_Eval_Quality.py").read_text()
        assert "12_Feedback_Loop.py" not in src

    def test_analysis_has_self_tuning_tab(self):
        src = (VIEWS / "3_Analysis.py").read_text()
        assert '"Self-Tuning"' in src
        assert "load_executor_params_history" in src
        # Honest dead-channel statuses (config#1841) replaced the permanent
        # scoring_weights not-found warning.
        assert "never written" in src
        assert "frozen since 2026-05-02" in src
        assert 'st.warning("scoring_weights.json not found in S3.")' not in src


class TestDailyNewsDemoted:
    def test_tab_on_signals_host(self):
        src = (VIEWS / "host_research_signals.py").read_text()
        assert '("Daily News", "Daily_News.py")' in src

    def test_no_standalone_nav_entry(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'page("Daily_News.py"' not in app_src
