"""Tests for components.judge_bias (config#1444 item 4)."""

from __future__ import annotations

from components.judge_bias import judge_bias_summary

_ROWS = [
    # agent A: haiku scores low (2,2), sonnet high (4,4) → divergence 2.0
    {"judged_agent_id": "A", "judge_model": "haiku", "score": 2},
    {"judged_agent_id": "A", "judge_model": "haiku", "score": 2},
    {"judged_agent_id": "A", "judge_model": "sonnet", "score": 4},
    {"judged_agent_id": "A", "judge_model": "sonnet", "score": 4},
    # agent B: both judges agree (~3) → low divergence
    {"judged_agent_id": "B", "judge_model": "haiku", "score": 3},
    {"judged_agent_id": "B", "judge_model": "sonnet", "score": 3},
    # a None/NaN score is skipped
    {"judged_agent_id": "B", "judge_model": "sonnet", "score": None},
]


class TestJudgeBiasSummary:
    def test_judges_and_overall(self):
        s = judge_bias_summary(_ROWS)
        assert s["judges"] == ["haiku", "sonnet"]
        assert s["overall"]["haiku"] == round((2 + 2 + 3) / 3, 3)
        assert s["overall"]["sonnet"] == round((4 + 4 + 3) / 3, 3)

    def test_per_agent_means_and_divergence(self):
        s = judge_bias_summary(_ROWS)
        per = {a["agent"]: a for a in s["per_agent"]}
        assert per["A"]["means"] == {"haiku": 2.0, "sonnet": 4.0}
        assert per["A"]["divergence"] == 2.0
        assert per["B"]["divergence"] == 0.0
        assert per["A"]["n"] == 4

    def test_most_divergent_first(self):
        s = judge_bias_summary(_ROWS)
        assert s["per_agent"][0]["agent"] == "A"  # divergence 2.0 sorts first

    def test_single_judge_divergence_none(self):
        s = judge_bias_summary([
            {"judged_agent_id": "X", "judge_model": "haiku", "score": 3},
        ])
        assert s["per_agent"][0]["divergence"] is None

    def test_empty(self):
        s = judge_bias_summary([])
        assert s == {"judges": [], "overall": {}, "per_agent": []}
