"""Tests for results/view_model.py — the skin-agnostic Crucible results layer
(config#1957). Pure functions: no streamlit, no S3."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from results import view_model as vm  # noqa: E402


def _card():
    return {
        "_provenance": {"run_date": "2026-07-04", "grader_source": "evaluator"},
        "tiles": {
            "predictor": {
                "status": "GREEN",
                "components": [
                    {
                        "name": "meta_l2_ic", "value": 0.212, "ci_low": 0.14,
                        "ci_high": 0.28, "n_samples": 1263, "target": 0.05,
                        "red_line": 0.0, "trend_decoration": "↑",
                        "criticality": "critical", "status": "GREEN",
                        "status_reason": "Leak-free CPCV IC clears target",
                    },
                    {
                        "name": "direction_veto_value", "value": None,
                        "status": "N/A-LOW-N", "criticality": "supporting",
                        "status_reason": "26 events < 40 floor",
                    },
                ],
            },
            "agent": {"status": "N/A-MISSING-INPUT", "components": []},
        },
    }


class TestIdentity:
    def test_reads_provenance(self):
        ident = vm.build_identity(_card(), "2026-07-04")
        assert ident["experiment_id"] == "reference-rate"
        assert ident["report_card_date"] == "2026-07-04"
        assert ident["backtest_date"] == "2026-07-04"
        assert len(ident["slots"]) == 3

    def test_absent_card_is_honest(self):
        ident = vm.build_identity(None, None)
        assert ident["report_card_date"] == vm.ABSENT
        assert ident["backtest_date"] == vm.ABSENT


class TestHeadline:
    def test_reads_producer_key_names(self):
        # portfolio_stats.json keys per vectorbt_bridge.portfolio_stats;
        # metrics.json keys per signal_quality overall (accuracy_21d/n_21d).
        eod = pd.DataFrame({"daily_alpha_pct": [0.5, -0.2, 0.3]})
        stats = vm.build_headline(
            eod,
            {"accuracy_21d": 0.542, "n_21d": 418},
            {"sharpe_ratio": 1.12, "max_drawdown": -0.068, "psr": 0.87},
        )
        by_label = {s["label"]: s for s in stats}
        assert by_label["Alpha vs SPY (cum)"]["value"] == "+0.60%"
        assert by_label["Sharpe (ann.)"]["value"] == "1.12"
        assert by_label["PSR"]["value"] == "0.87"
        assert by_label["Hit rate · 21d"]["value"] == "54.2%"
        assert "418" in by_label["Hit rate · 21d"]["sub"]
        assert by_label["Max drawdown"]["value"] == "-6.8%"

    def test_absent_sources_render_absent_not_zero(self):
        stats = vm.build_headline(None, None, None)
        assert all(s["value"] == vm.ABSENT for s in stats)
        assert all(s["help"] for s in stats)  # explainer layer always present


class TestEquityFrame:
    def test_compounds_ledger_returns(self):
        eod = pd.DataFrame({
            "date": ["2026-01-02", "2026-01-03"],
            "daily_return_pct": [1.0, 1.0],
            "spy_return_pct": [0.5, 0.5],
        })
        eq = vm.equity_frame(eod)
        assert list(eq.columns) == ["date", "Portfolio", "SPY"]
        assert round(eq["Portfolio"].iloc[-1], 4) == 2.01  # (1.01^2 - 1) * 100

    def test_missing_columns_yield_empty(self):
        assert vm.equity_frame(pd.DataFrame({"date": ["2026-01-02"]})).empty
        assert vm.equity_frame(None).empty


class TestAttribution:
    def test_prefers_21d_target_and_carries_fdr(self):
        attribution = {"correlations": {
            "quant_score": {
                "beat_spy_5d": 0.02, "beat_spy_5d_fdr_significant": False,
                "log_alpha_21d": 0.16, "log_alpha_21d_fdr_significant": True,
            },
            "conviction": {"log_alpha_21d": -0.03, "log_alpha_21d_fdr_significant": False},
        }}
        rows = vm.attribution_rows(attribution)
        assert rows[0]["sub_score"] == "quant_score"  # sorted by |corr|
        assert rows[0]["target"] == "log_alpha_21d"
        assert rows[0]["fdr_significant"] is True
        assert rows[1]["fdr_significant"] is False

    def test_absent_or_malformed_yields_empty(self):
        assert vm.attribution_rows(None) == []
        assert vm.attribution_rows({"correlations": "oops"}) == []


class TestIntegrity:
    def test_absent_artifact_is_an_explicit_row(self):
        rows = vm.integrity_rows(None, {"status": "ok", "note": "n=418 vs floor 300"}, None, None)
        assert len(rows) == 4  # every leg present regardless of artifact availability
        assert rows[0]["status"] == "ABSENT"
        assert rows[1]["status"] == "OK"
        assert "418" in rows[1]["detail"]

    def test_pit_parity_headline_delta_reported_not_adjudicated(self):
        # Live schema pit_parity-1.x carries no status; the view reports the
        # producer's headline delta and never grades it itself.
        rows = vm.integrity_rows({"headline_log_alpha_delta": -0.144}, None, None, None)
        assert rows[0]["status"] == "REPORTED"
        assert "-0.144" in rows[0]["detail"]


class TestMetricRows:
    def test_full_contract_rendered(self):
        rows = vm.metric_rows(_card(), "predictor")
        assert rows[0]["metric"] == "meta_l2_ic"
        assert rows[0]["ci"] == "[0.14, 0.28]"
        assert rows[0]["n"] == 1263
        assert rows[0]["reason"] == "Leak-free CPCV IC clears target"
        # honest N/A row keeps its reason, value renders ABSENT
        assert rows[1]["value"] == vm.ABSENT
        assert rows[1]["status"] == "N/A-LOW-N"

    def test_tile_labels_follow_card(self):
        labels = dict(vm.tile_labels(_card()))
        assert labels["predictor"] == "Predictor"
        assert vm.tile_labels(None) == []


class TestAlphaByPeriod:
    def _eod(self):
        return pd.DataFrame({
            # two trading weeks: Mar 9-13 and Mar 16-20 (2026)
            "date": ["2026-03-09", "2026-03-13", "2026-03-16", "2026-03-20"],
            "daily_alpha_pct": [0.5, -0.2, 1.0, 0.5],
        })

    def test_weekly_buckets_to_week_ending_friday(self):
        out = vm.alpha_by_period(self._eod(), "W")
        assert list(out["alpha_pct"].round(2)) == [0.3, 1.5]
        assert list(out["n_days"]) == [2, 2]
        assert out["label"].iloc[0].strftime("%Y-%m-%d") == "2026-03-13"

    def test_daily_passthrough(self):
        out = vm.alpha_by_period(self._eod(), "D")
        assert len(out) == 4
        assert (out["n_days"] == 1).all()

    def test_monthly_and_bad_period(self):
        out = vm.alpha_by_period(self._eod(), "M")
        assert len(out) == 1 and round(out["alpha_pct"].iloc[0], 2) == 1.8
        assert vm.alpha_by_period(self._eod(), "Q").empty
        assert vm.alpha_by_period(None, "W").empty


class TestRollingAlpha:
    def test_window_smoothing(self):
        eod = pd.DataFrame({
            "date": pd.date_range("2026-03-09", periods=5, freq="B").astype(str),
            "daily_alpha_pct": [1.0, 0.0, 1.0, 0.0, 1.0],
        })
        out = vm.rolling_alpha_frame(eod, window=2)
        assert len(out) == 4
        assert list(out["rolling_mean"]) == [0.5, 0.5, 0.5, 0.5]

    def test_short_history_is_empty_not_partial(self):
        eod = pd.DataFrame({"date": ["2026-03-09"], "daily_alpha_pct": [1.0]})
        assert vm.rolling_alpha_frame(eod, window=20).empty
