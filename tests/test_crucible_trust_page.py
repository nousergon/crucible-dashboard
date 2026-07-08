"""Trust page + battery registry tests (config#1958 deliverable 10)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from results import view_model as vm  # noqa: E402
from results.battery_registry import BATTERY_FINDINGS, BATTERY_LEGS  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


class TestRegistryShape:
    def test_legs_carry_required_fields(self):
        for leg in BATTERY_LEGS:
            for field in ("key", "title", "repo", "workflow", "tests", "proves"):
                assert leg.get(field), f"{leg.get('key')}: missing {field}"
            assert leg["repo"] in ("crucible-backtester", "crucible-evaluator")
            assert all(t.endswith(".py") for t in leg["tests"])

    def test_leg_keys_unique(self):
        keys = [leg["key"] for leg in BATTERY_LEGS]
        assert len(keys) == len(set(keys))

    def test_findings_are_anchored_to_fixes(self):
        # A finding without a checkable receipt is marketing, not provenance.
        for f in BATTERY_FINDINGS:
            assert f.get("fix") and "#" in f["fix"], f
            assert f.get("date") and f.get("found_by") and f.get("finding")

    def test_finding_found_by_names_a_registered_leg(self):
        keys = {leg["key"] for leg in BATTERY_LEGS}
        for f in BATTERY_FINDINGS:
            assert f["found_by"] in keys, f["found_by"]


class TestTrustRows:
    def test_joins_ci_verdict_per_repo(self):
        verdicts = {
            "crucible-backtester": {"conclusion": "success", "head_sha": "abc1234",
                                    "updated_at": "2026-07-08 20:00", "html_url": "https://x"},
            "crucible-evaluator": {"conclusion": "unavailable", "error": "no token"},
        }
        rows = vm.trust_rows(BATTERY_LEGS, verdicts)
        assert len(rows) == len(BATTERY_LEGS)
        by_repo = {r["repo"]: r for r in rows}
        assert by_repo["crucible-backtester"]["ci"] == "SUCCESS"
        assert by_repo["crucible-evaluator"]["ci"] == "UNAVAILABLE"
        assert by_repo["crucible-evaluator"]["error"] == "no token"

    def test_unqueried_repo_is_explicit_not_dropped(self):
        rows = vm.trust_rows(BATTERY_LEGS, {})
        assert all(r["ci"] == "UNAVAILABLE" for r in rows)


class TestPageWiring:
    def test_trust_tab_registered_on_host(self):
        src = (REPO_ROOT / "views" / "host_crucible_results.py").read_text()
        assert '("Trust", "Crucible_Trust.py")' in src

    def test_trust_view_renders_via_view_model_and_registry(self):
        src = (REPO_ROOT / "views" / "Crucible_Trust.py").read_text()
        assert "from results import view_model" in src
        assert "BATTERY_LEGS" in src and "BATTERY_FINDINGS" in src

    def test_trust_view_carries_honest_boundaries(self):
        src = (REPO_ROOT / "views" / "Crucible_Trust.py").read_text()
        assert "does not prove" in src.lower()
        assert "paper-traded" in src.lower()
