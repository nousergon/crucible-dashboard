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


class TestExecutionBuilders:
    def test_headline_from_live_shapes(self):
        # Key names verified against live backtest/2026-07-02 artifacts.
        stats = vm.execution_headline(
            {"summary": {"total_entries": 145}},
            {"summary": {"n_roundtrips": 217, "win_rate": 0.668, "winsorized_capture_ratio": 0.24}},
            {"guard_lift": -0.002, "assessment": "neutral"},
        )
        by_label = {s["label"]: s for s in stats}
        assert by_label["Entries (window)"]["value"] == "145"
        assert by_label["Win rate"]["value"] == "66.8%"
        assert by_label["Risk-guard lift"]["value"] == "-0.002"
        assert by_label["Risk-guard lift"]["sub"] == "neutral"

    def test_headline_absent_everywhere(self):
        assert all(s["value"] == vm.ABSENT for s in vm.execution_headline(None, None, None))

    def test_trigger_and_exit_rows_tolerate_nulls(self):
        trig = vm.trigger_rows({"triggers": [
            {"trigger": "other", "n_trades": 48, "avg_slippage_vs_signal": 0.0845,
             "avg_slippage_vs_open": 0.0158, "win_rate_vs_spy": None},
        ]})
        assert trig[0]["slippage_vs_signal"] == "+0.08%"
        assert trig[0]["win_rate_vs_spy"] == vm.ABSENT
        exits = vm.exit_type_rows({"by_exit_type": [
            {"exit_type": "atr_trailing_stop", "n": 8, "avg_mfe": 3.71,
             "avg_mae": -3.34, "avg_realized": -1.18, "avg_capture": -1.44},
        ]})
        assert exits[0]["avg_mae"] == "-3.34%"

    def test_shadow_classification(self):
        rows = vm.shadow_classification_rows({"classification": {
            "precision": 0.6321, "recall": 0.9165, "f1": 0.7482, "accuracy": 0.6148, "n": 1342,
        }})
        by = {r["measure"]: r["value"] for r in rows}
        assert by["Precision (traded → beat SPY)"] == "63.2%"
        assert by["N classified"] == "1342"
        assert vm.shadow_classification_rows(None) == []


class TestFeedbackBuilders:
    def test_apply_audit_rows_schema_v1(self):
        rows = vm.apply_audit_rows({"schema_version": 1, "as_of": "2026-07-11", "loops": {
            "scoring_weights": {"outcome": "blocked", "blocked_by": ["significance_floor"],
                                "consecutive_blocked_weeks": 3},
            "executor_params": {"outcome": "promoted", "blocked_by": None,
                                "consecutive_blocked_weeks": 0},
        }})
        assert [r["loop"] for r in rows] == ["executor_params", "scoring_weights"]
        assert rows[1]["blocked_by"] == "significance_floor"
        assert rows[0]["outcome"] == "promoted"
        assert vm.apply_audit_rows(None) == []

    def test_config_snapshot_states_never_written_honestly(self):
        rows = vm.config_snapshot_rows({
            "executor_params": {"present": True, "last_modified": "2026-07-03", "keys": ["a", "b"]},
            "scoring_weights": {"present": False},
        })
        by = {r["config"]: r for r in rows}
        assert by["executor_params"]["state"] == "LIVE"
        assert by["scoring_weights"]["state"] == "NEVER WRITTEN"
        assert by["scoring_weights"]["last_written"] == vm.ABSENT
