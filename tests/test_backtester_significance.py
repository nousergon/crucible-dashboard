"""Tests for components.backtester_significance — the console rendering of the
backtester's observe-first significance verdicts (config#1444 items 1+2).

Pure-helper tests only (the render funcs are thin Streamlit wrappers).
"""

from __future__ import annotations

from components.backtester_significance import (
    build_trend_rows,
    count_undefended,
    evidence_summary,
    significance_observe_rows,
    trend_row,
)

# Record shapes mirror evaluate._collect_significance_observe output.
_WEIGHT = {
    "gate": "weight_optimizer", "significant": False, "would_block": True,
    "did_promote": True, "promotes_on_undefended_evidence": True, "enforced": False,
    "detail": {"per_subscore": {"quant": {"significant": False}, "qual": {"significant": False}}, "n_test": 120},
}
_PRED_SIZING = {
    "gate": "predictor_sizing", "significant": True, "would_block": False,
    "did_promote": True, "promotes_on_undefended_evidence": False, "enforced": False,
    "detail": {"status": "ok", "n": 200, "ic": 0.12, "p_value": 0.01,
               "ci_low": 0.05, "ci_high": 0.2, "method": "bootstrap_ic_ci + spearman_p"},
}
_VETO = {
    "gate": "veto_analysis", "significant": True, "would_block": False,
    "did_promote": False, "promotes_on_undefended_evidence": False, "enforced": False,
    "detail": {"status": "ok", "n": 100, "rate": 0.9, "base_rate": 0.5,
               "ci_low": 0.82, "ci_high": 0.95, "method": "wilson_lower_bound_vs_base_rate"},
}
_STANCE = {
    "gate": "stance_sizing", "significant": True, "would_block": False,
    "did_promote": True, "promotes_on_undefended_evidence": False, "enforced": False,
    "detail": {"status": "ok", "estimate": 0.04, "ci_low": 0.01, "ci_high": 0.07,
               "best_stance": "momentum", "worst_stance": "value",
               "method": "two_sample_mean_diff_bootstrap"},
}
_BARRIER_DORMANT = {
    "gate": "barrier_sizing", "significant": False, "would_block": True,
    "did_promote": False, "promotes_on_undefended_evidence": False, "enforced": False,
    "detail": {"status": "insufficient_data", "n_a": 0, "n_b": 0, "significant": False},
}


class TestRows:
    def test_full_block_rows_and_order(self):
        sig = {
            "weight_result": _WEIGHT, "veto_result": _VETO,
            "predictor_sizing": _PRED_SIZING, "barrier_sizing": _BARRIER_DORMANT,
            "stance_sizing": _STANCE,
        }
        rows = significance_observe_rows(sig)
        # All five present, stable label order.
        assert [r["Optimizer"] for r in rows] == [
            "Scoring weights", "Predictor veto", "Predictor sizing",
            "Barrier sizing", "Stance sizing",
        ]
        weight = rows[0]
        assert weight["⚠"] == "⚠ UNDEFENDED"
        assert weight["Would block"] == "yes"
        assert weight["Promoted (live)"] == "yes"

    def test_insufficient_is_not_a_verdict(self):
        rows = significance_observe_rows({"barrier_sizing": _BARRIER_DORMANT})
        assert rows[0]["Significant?"] == "n/a (insufficient)"
        assert rows[0]["Would block"] == "—"
        assert rows[0]["⚠"] == ""

    def test_missing_optimizer_skipped(self):
        assert significance_observe_rows({"weight_result": _WEIGHT}) and len(
            significance_observe_rows({"weight_result": _WEIGHT})) == 1
        assert significance_observe_rows(None) == []
        assert significance_observe_rows({}) == []


class TestCountUndefended:
    def test_counts(self):
        sig = {"weight_result": _WEIGHT, "predictor_sizing": _PRED_SIZING,
               "barrier_sizing": _BARRIER_DORMANT}
        undefended, with_verdict = count_undefended(sig)
        assert undefended == 1            # only weight is undefended
        assert with_verdict == 2          # weight + pred have verdicts; barrier insufficient

    def test_empty(self):
        assert count_undefended(None) == (0, 0)


class TestEvidenceSummary:
    def test_ic(self):
        s = evidence_summary(_PRED_SIZING["detail"])
        assert "IC=0.120" in s and "CI[" in s and "p=0.010" in s

    def test_wilson(self):
        s = evidence_summary(_VETO["detail"])
        assert "precision=0.900" in s and "base=0.500" in s

    def test_mean_diff(self):
        s = evidence_summary(_STANCE["detail"])
        assert "momentum vs value" in s and "Δμ=0.040" in s

    def test_weight_per_subscore(self):
        s = evidence_summary(_WEIGHT["detail"])
        assert "none" in s and "n_test=120" in s

    def test_none(self):
        assert evidence_summary(None) == "—"


class TestTrend:
    def test_trend_row(self):
        metrics = {
            "simulation": {"sharpe_ratio": 1.4}, "accuracy_10d": 0.53,
            "avg_alpha_10d": 0.006,
            "significance_observe": {"weight_result": _WEIGHT},  # 1 undefended
        }
        row = trend_row("2026-07-04", metrics)
        assert row == {"date": "2026-07-04", "sharpe": 1.4, "accuracy_10d": 0.53,
                       "avg_alpha_10d": 0.006, "n_undefended": 1}

    def test_build_trend_rows_sorted(self):
        per_date = {
            "2026-07-11": {"accuracy_10d": 0.55},
            "2026-07-04": {"accuracy_10d": 0.53},
        }
        rows = build_trend_rows(per_date)
        assert [r["date"] for r in rows] == ["2026-07-04", "2026-07-11"]  # oldest→newest
        assert rows[0]["n_undefended"] == 0  # no significance block → 0
