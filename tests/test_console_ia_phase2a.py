"""Wiring guards for console-IA phase 2a (alpha-engine-config#1987).

System Health page broken up + Intraday Surveillance retired. Source-text
assertions in the repo's usual style (page modules are not imported — their
module-level Streamlit calls need a live runtime).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VIEWS = REPO_ROOT / "views"


class TestSystemHealthRetired:
    def test_page_deleted(self):
        assert not (VIEWS / "4_System_Health.py").exists()

    def test_host_no_longer_lists_it(self):
        host_src = (VIEWS / "host_system_health.py").read_text()
        assert "4_System_Health.py" not in host_src

    def test_host_filename_and_key_survive_for_deep_links(self):
        # The Fleet Status deep-link /host_system_health?tab=Backlog+Groom is
        # pinned by TestDeepLinkTargets — the retirement must not rename the
        # host file or its key.
        host_src = (VIEWS / "host_system_health.py").read_text()
        assert 'key="host_system_health"' in host_src
        assert '("Backlog Groom", "42_Backlog_Groom.py")' in host_src

    def test_data_and_maturity_rehomed_under_observability(self):
        assert (VIEWS / "Data_and_Maturity.py").exists()
        host_src = (VIEWS / "host_observability.py").read_text()
        assert '("Data & Maturity", "Data_and_Maturity.py")' in host_src

    def test_live_optimizer_params_rehomed_to_analysis(self):
        src = (VIEWS / "3_Analysis.py").read_text()
        assert "Live Optimizer Params" in src
        assert "load_executor_params" in src

    def test_maturity_names_dead_write_paths_honestly(self):
        # config#1841: only executor_params.json ever promoted. The maturity
        # table must not present the dead channels as an "Active" loop.
        src = (VIEWS / "Data_and_Maturity.py").read_text()
        assert "NEVER promoted (config#1841)" in src
        assert "config/scoring_weights.json — never written" in src
        assert "config/predictor_params.json — never written" in src
        assert "config/research_params.json — stale since 2026-05-02" in src


class TestIntradaySurveillanceRetired:
    def test_page_deleted(self):
        assert not (VIEWS / "22_Intraday_Surveillance.py").exists()

    def test_no_nav_entry_remains(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert "22_Intraday_Surveillance.py" not in app_src

    def test_raw_snapshots_rehomed_to_fleet_status(self):
        src = (VIEWS / "48_Fleet_Status.py").read_text()
        assert "load_intraday_heartbeat" in src
        assert "load_intraday_latest_prices" in src
        assert "intraday/heartbeat.json" in src
        assert "intraday/latest_prices.json" in src
