"""Tests for the Merged PRs console page + pr_merge_loader."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import pr_merge_loader  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


class TestClassifyMergeSource:
    def test_recorded_attribution_wins(self):
        row = {"repo": "nousergon/crucible-predictor", "number": 1, "title": "x"}
        attr = {"nousergon/crucible-predictor#1": {"merge_source": "agent"}}
        assert pr_merge_loader.classify_merge_source(row, attr) == ("agent", "recorded")

    def test_dependabot_author(self):
        row = {
            "repo": "nousergon/krepis", "number": 2, "title": "bump deps",
            "author": "dependabot[bot]", "merged_by": "cipher813", "labels": [],
        }
        assert pr_merge_loader.classify_merge_source(row, {})[0] == "dependabot"

    def test_agent_merged_label(self):
        row = {
            "repo": "nousergon/crucible-executor", "number": 3, "title": "fix",
            "author": "cipher813", "labels": ["agent-merged"],
        }
        assert pr_merge_loader.classify_merge_source(row, {}) == ("agent", "labeled")

    def test_groom_title_prefix_defaults_human_without_record(self):
        # Groom PRs often carry [P0–P3]/tier titles — humans merge these too.
        row = {
            "repo": "nousergon/telos", "number": 19,
            "title": "[P2/high] feat(engine): Form 2210 penalty",
            "author": "cipher813", "labels": [],
        }
        assert pr_merge_loader.classify_merge_source(row, {}) == ("human", "default")

    def test_default_human(self):
        row = {
            "repo": "nousergon/crucible-dashboard", "number": 5,
            "title": "feat: manual change", "author": "cipher813", "labels": [],
        }
        assert pr_merge_loader.classify_merge_source(row, {}) == ("human", "default")


class TestNavRegistration:
    def test_host_registers_merged_prs_page(self):
        text = (REPO_ROOT / "views" / "host_system_health.py").read_text()
        assert "47_Merged_PRs.py" in text
        assert "Merged PRs" in text


class TestLoadMergedPrs:
    def test_load_merged_prs_enriches_rows(self):
        search_payload = {
            "search": {
                "issueCount": 1,
                "nodes": [{
                    "number": 339,
                    "title": "Bump nousergon-lib to v0.83.0",
                    "url": "https://github.com/nousergon/crucible-predictor/pull/339",
                    "mergedAt": "2026-07-05T17:26:56Z",
                    "author": {"login": "cipher813"},
                    "mergedBy": {"login": "cipher813"},
                    "labels": {"nodes": []},
                    "repository": {"nameWithOwner": "nousergon/crucible-predictor"},
                }],
            },
        }
        attr = {"nousergon/crucible-predictor#339": {"merge_source": "agent"}}
        with patch.object(pr_merge_loader, "load_merge_attribution", return_value=attr), \
                patch.object(pr_merge_loader, "_github_graphql", return_value=search_payload), \
                patch.object(pr_merge_loader, "_github_token", return_value="tok"):
            rows, count = pr_merge_loader.load_merged_prs(days=14)
        assert count == 1
        assert len(rows) == 1
        assert rows[0]["merge_source"] == "agent"
        assert rows[0]["confidence"] == "recorded"
        assert rows[0]["pr"] == "#339"
