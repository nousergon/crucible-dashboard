"""Wiring tests for the Crucible Results surface (config#1957).

Mirrors test_experiments_page.py: the page modules are NOT imported (their
module-level Streamlit calls need a live runtime) — nav registration and
page wiring are asserted against source text.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent


class TestNavRegistration:
    def test_host_registered_in_app_nav(self):
        src = (REPO_ROOT / "app.py").read_text()
        assert 'page("host_crucible_results.py", "Crucible Results"' in src

    def test_host_lists_all_subviews(self):
        src = (REPO_ROOT / "views" / "host_crucible_results.py").read_text()
        for label, filename in [
            ("Overview", "Crucible_Overview.py"),
            ("Validation", "Crucible_Validation.py"),
            ("Evaluation", "Crucible_Evaluation.py"),
            ("Execution", "Crucible_Execution.py"),
            ("Feedback loop", "Crucible_Feedback.py"),
        ]:
            assert f'("{label}", "{filename}")' in src

    def test_subview_files_exist(self):
        for filename in (
            "Crucible_Overview.py", "Crucible_Validation.py", "Crucible_Evaluation.py",
            "Crucible_Execution.py", "Crucible_Feedback.py",
        ):
            assert (REPO_ROOT / "views" / filename).exists(), filename


class TestPageWiring:
    def test_views_render_through_shared_view_model(self):
        # One renderer, two skins (plan §4.1): every Crucible view must go
        # through results.view_model, never compute display values inline.
        for filename in (
            "Crucible_Overview.py", "Crucible_Validation.py", "Crucible_Evaluation.py",
            "Crucible_Execution.py", "Crucible_Feedback.py",
        ):
            src = (REPO_ROOT / "views" / filename).read_text()
            assert "from results import view_model" in src, filename

    def test_overview_never_renders_ops_tiles(self):
        # Plan §9.2: the 8-tile ops report card is console-only; the prosumer
        # Overview renders experiment verdicts + the integrity strip instead.
        src = (REPO_ROOT / "views" / "Crucible_Overview.py").read_text()
        assert "render_overview" not in src
        assert "experiment_tile_verdicts" in src
        assert "Measurement integrity" in src

    def test_overview_carries_disclaimer(self):
        # Plan §8.3: illustrative-only framing is mandatory on the surface.
        src = (REPO_ROOT / "views" / "Crucible_Overview.py").read_text()
        assert "illustrative only" in src.lower()
        assert "not investment advice" in src.lower()

    def test_validation_reads_integrity_artifacts(self):
        src = (REPO_ROOT / "views" / "Crucible_Validation.py").read_text()
        for artifact in (
            "pit_parity.json", "sample_size.json",
            "walk_forward_stability.json", "optimizer_churn.json",
            "attribution.json", "signal_quality.csv",
        ):
            assert artifact in src, artifact

    def test_view_model_is_streamlit_free(self):
        # The layer the public /dash skin will reuse must stay pure.
        src = (REPO_ROOT / "results" / "view_model.py").read_text()
        assert "import streamlit" not in src
        assert "import boto3" not in src
