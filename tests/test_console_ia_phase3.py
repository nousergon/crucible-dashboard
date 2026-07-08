"""Wiring guards for console-IA phase 3 (alpha-engine-config#1989).

Home slimmed to a pure triage router: one status truth (the fleet resolver),
KPIs sourced from the eod_report headline, no reads of never-produced
artifacts. Source-text assertions in the repo's usual style.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
APP_SRC = (REPO_ROOT / "app.py").read_text()


class TestSingleStatusTruth:
    def test_no_direct_health_json_reads(self):
        # The fleet-strip resolver is the one status truth on home; the old
        # Pipeline banner's raw health/{module}.json reads are gone.
        assert 'health/{module_name}.json' not in APP_SRC
        assert "_render_status_banner" not in APP_SRC
        assert "_load_module_health" not in APP_SRC.replace(
            "# The old health/*.json Pipeline banner (and its _load_module_health reader)", ""
        )

    def test_fleet_strip_remains(self):
        assert "_render_fleet_strip" in APP_SRC
        assert "resolve_fleet" in APP_SRC


class TestKpiSourceAlignment:
    def test_key_metrics_read_the_eod_report_headline(self):
        # Home and /eod-report must share one computation path for NAV +
        # daily alpha (both read eod_report.json's summary).
        assert "load_eod_report" in APP_SRC
        assert "list_eod_report_dates" in APP_SRC
        assert 'summary.get("daily_alpha_pct")' in APP_SRC
        assert 'summary.get("spy_close_provisional")' in APP_SRC


class TestNoDeadArtifactReads:
    def test_predictor_params_read_is_gone(self):
        # config/predictor_params.json has never been written (config#1841 /
        # I1984 item 4a) — home must not present its fallback as state. The
        # docstring explaining the removal may mention the filename; an
        # actual READ (fetch of the key / the loader) must not appear.
        assert '"config/predictor_params.json"' not in APP_SRC
        assert "load_predictor_params" not in APP_SRC
        assert "veto_confidence" not in APP_SRC

    def test_activity_row_links_to_execution(self):
        assert "/host_execution?tab=Order+Book" in APP_SRC


class TestRegimeChip:
    def test_market_context_grid_replaced_by_chip(self):
        assert "_render_regime_chip" in APP_SRC
        assert "_render_market_context" not in APP_SRC
        assert "/host_predictor?tab=Regime" in APP_SRC
