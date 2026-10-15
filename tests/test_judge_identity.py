"""Tests for loaders.judge_identity (alpha-engine-config-I11484)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loaders.judge_identity import grader_label, served_grader  # noqa: E402


class TestServedGrader:
    def test_reads_the_router_resolved_model_not_the_tier(self):
        art = {
            "judge_model": "claude-haiku-4-5",
            "judge_request_model": "low-deepseek-v4-flash-tools",
            "judge_resolved_model": "deepseek-v4-flash",
        }
        assert served_grader(art) == "deepseek-v4-flash"

    def test_absent_is_none_never_the_tier_key(self):
        # A pre-I11484 record with no served model must not be reported as
        # graded by the tier's namesake.
        assert served_grader({"judge_model": "claude-sonnet-4-6"}) is None

    def test_blank_or_non_string_is_none(self):
        assert served_grader({"judge_resolved_model": "  "}) is None
        assert served_grader({"judge_resolved_model": None}) is None
        assert served_grader({"judge_resolved_model": 42}) is None

    def test_strips_whitespace(self):
        assert served_grader({"judge_resolved_model": " deepseek-v4-flash\n"}) == "deepseek-v4-flash"


class TestGraderLabel:
    def test_served_model_wins(self):
        assert grader_label("claude-haiku-4-5", "deepseek-v4-flash") == "deepseek-v4-flash"

    def test_unrecorded_is_marked_and_carries_the_tier(self):
        label = grader_label("claude-haiku-4-5", None)
        assert label == "unrecorded (tier claude-haiku-4-5)"
        assert label != "claude-haiku-4-5"

    def test_unrecorded_without_tier(self):
        assert grader_label(None, None) == "unrecorded (tier —)"
