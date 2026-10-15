"""Tests for components.judge_bias (config#1444 item 4)."""

from __future__ import annotations

from components.judge_bias import judge_bias_summary

def _row(agent, served, score, tier="tier-x"):
    return {
        "judged_agent_id": agent,
        "judge_model": tier,
        "judge_served_model": served,
        "score": score,
    }


_ROWS = [
    # agent A: haiku scores low (2,2), sonnet high (4,4) → divergence 2.0
    _row("A", "haiku", 2),
    _row("A", "haiku", 2),
    _row("A", "sonnet", 4),
    _row("A", "sonnet", 4),
    # agent B: both judges agree (~3) → low divergence
    _row("B", "haiku", 3),
    _row("B", "sonnet", 3),
    # a None/NaN score is skipped
    _row("B", "sonnet", None),
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
        s = judge_bias_summary([_row("X", "haiku", 3)])
        assert s["per_agent"][0]["divergence"] is None

    def test_empty(self):
        s = judge_bias_summary([])
        assert s == {"judges": [], "overall": {}, "per_agent": []}


class TestGroupsByServedGrader:
    """alpha-engine-config-I11484: the grader is the served model, not the tier."""

    def test_two_tiers_served_by_one_model_are_one_grader(self):
        # The 2026-10-10 weekly: first pass and escalation on different tiers,
        # both served by the same model. That is ONE grader, so there is no
        # cross-judge divergence to report.
        rows = [
            _row("A", "deepseek-v4-flash", 2, tier="claude-haiku-4-5"),
            _row("A", "deepseek-v4-flash", 4, tier="claude-sonnet-4-6"),
        ]
        s = judge_bias_summary(rows)
        assert s["judges"] == ["deepseek-v4-flash"]
        assert s["overall"] == {"deepseek-v4-flash": 3.0}
        assert s["per_agent"][0]["divergence"] is None

    def test_distinct_served_models_on_one_tier_are_two_graders(self):
        rows = [
            _row("A", "claude-haiku-4-5-20251001", 2, tier="claude-haiku-4-5"),
            _row("A", "deepseek-v4-flash", 4, tier="claude-haiku-4-5"),
        ]
        s = judge_bias_summary(rows)
        assert s["judges"] == ["claude-haiku-4-5-20251001", "deepseek-v4-flash"]
        assert s["per_agent"][0]["divergence"] == 2.0

    def test_unrecorded_served_model_is_not_credited_to_the_tier(self):
        rows = [
            {"judged_agent_id": "A", "judge_model": "claude-sonnet-4-6", "score": 3},
        ]
        s = judge_bias_summary(rows)
        assert s["judges"] == ["unrecorded (tier claude-sonnet-4-6)"]
        assert "claude-sonnet-4-6" not in s["overall"]
