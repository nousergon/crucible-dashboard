"""Tests for the Experiments console page + its leaderboard loaders
(config#1685 — champion/challenger ablation ledgers).

Mirrors test_think_tank_page.py: streamlit is mocked (cache_data →
passthrough) and the page module itself is NOT imported (its module-level
Streamlit calls need a live runtime) — page wiring is asserted against
source text instead.
"""

import ast
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import s3_loader  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


def _extract_literal(src: str, name: str):
    """Pull a module-level ``name = <literal>`` assignment out of source text
    and ``ast.literal_eval`` it, without importing the module (its
    module-level Streamlit calls — ``st.tabs()`` unpacked into 3 names, etc.
    — need a live runtime; see the module docstring above)."""
    match = re.search(rf"^{re.escape(name)} = ", src, re.MULTILINE)
    assert match, f"{name} not found as a module-level assignment"
    start = match.end()
    opens, closes = "([{", ")]}"
    pair = dict(zip(closes, opens))
    stack: list[str] = []
    end = None
    for i, ch in enumerate(src[start:], start=start):
        if ch in opens:
            stack.append(ch)
        elif ch in closes:
            assert stack and stack[-1] == pair[ch], f"unbalanced brackets parsing {name}"
            stack.pop()
            if not stack:
                end = i + 1
                break
    assert end is not None, f"could not find closing bracket for {name}"
    return ast.literal_eval(src[start:end])


class TestLeaderboardLoaders:
    def test_list_leaderboard_dates_strips_flat_json_keys(self):
        pages = [{"Contents": [
            {"Key": "research/producer_leaderboard/2026-06-30.json"},
            {"Key": "research/producer_leaderboard/2026-07-02.json"},
            {"Key": "research/producer_leaderboard/latest.json"},  # not a date
        ]}]
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = pages
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            dates = s3_loader.list_leaderboard_dates("research/producer_leaderboard/")
        assert dates == ["2026-06-30", "2026-07-02"]

    def test_load_leaderboard_reads_dated_key(self):
        payload = {
            "leaderboard_id": "producer",
            "date": "2026-07-02",
            "n_dates": 0,
            "specs": [
                {"name": "agentic_sector_teams", "kind": "champion",
                 "realized_rank_ic": None, "n_dates_scored": 0},
            ],
        }
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or payload):
            lb = s3_loader.load_leaderboard("research/producer_leaderboard/", "2026-07-02")
        assert lb == payload
        assert captured == ["research/producer_leaderboard/2026-07-02.json"]

    def test_list_shadow_cohort_dates_uses_date_prefixes(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "list_s3_prefixes",
                             return_value=["2026-07-02"]) as lsp:
            dates = s3_loader.list_shadow_cohort_dates("signals_shadow/no_agent_quant/")
        assert dates == ["2026-07-02"]
        lsp.assert_called_once_with("b", "signals_shadow/no_agent_quant/")


class TestChampionLoopLoaders:
    """config#2364/#2367/#2369 — champion/challenger promotion loop."""

    def test_load_champion_pointer_reads_expected_key(self):
        payload = {"schema_version": 1, "champion": "scanner_predictor_direct",
                   "promoted_at": "2026-07-13T22:07:09+00:00",
                   "promotion_source": "operator_bootstrap"}
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or payload):
            pointer = s3_loader.load_champion_pointer()
        assert pointer == payload
        assert captured == ["config/producer_champion.json"]

    def test_list_champion_audit_dates_strips_flat_json_keys(self):
        pages = [{"Contents": [
            {"Key": "config/apply_audit/producer_champion/2026-07-13.json"},
            {"Key": "config/apply_audit/producer_champion/latest.json"},  # not a date
        ]}]
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = pages
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            dates = s3_loader.list_champion_audit_dates()
        assert dates == ["2026-07-13"]

    def test_load_champion_audit_reads_dated_key(self):
        payload = {"schema_version": 1, "date": "2026-07-13", "outcome": "promoted"}
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or payload):
            audit = s3_loader.load_champion_audit("2026-07-13")
        assert audit == payload
        assert captured == ["config/apply_audit/producer_champion/2026-07-13.json"]

    def test_load_champion_audit_latest_reads_latest_key(self):
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or {}):
            s3_loader.load_champion_audit_latest()
        assert captured == ["config/apply_audit/producer_champion/latest.json"]

    def test_champion_leaderboard_key_distinct_from_research_producer_leaderboard(self):
        """config#2452 regression guard on the dashboard side too: the
        console must read the champion-gate's own key, never the one
        crucible-research's producer leaderboard writes to."""
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or {}):
            s3_loader.load_champion_leaderboard("2026-07-13")
        assert captured == ["research/producer_leaderboard_champion_gate/2026-07-13.json"]
        assert captured[0] != "research/producer_leaderboard/2026-07-13.json"


class TestPageWiring:
    """Source-text pins (the page module needs a live Streamlit runtime)."""

    def test_page_exists_and_reads_both_leaderboards(self):
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        assert "research/producer_leaderboard/" in src
        assert "scanner/leaderboard/" in src
        assert "signals_shadow/no_agent_quant/" in src
        assert "signals_shadow/single_agent_quant/" in src
        assert "candidates_shadow/momentum_sleeve/" in src
        # Honest empty state: the page must distinguish maturing from broken.
        assert "matur" in src.lower()

    def test_page_wires_champion_loop_tab(self):
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        assert "champion_tab" in src
        assert "_render_champion_loop" in src
        assert "load_champion_pointer" in src

    def test_nav_registers_experiments_section(self):
        src = (REPO_ROOT / "app.py").read_text()
        assert "Experiments" in src, "app.py must carry the Experiments section"
        assert "46_Experiments.py" in src, (
            "app.py st.navigation must register views/46_Experiments.py — "
            "an unregistered view is unreachable on the console (config#1685)"
        )


class TestChampionVocabularyParity:
    """Guards ``views/46_Experiments.py``'s champion-arm and blocked-by
    vocabulary against drift from the live promotion engine
    (alpha-engine-config-I6431).

    LIMITATION: alpha-engine-backtester (which owns
    ``optimizer/champion_promotion.py::VALID_CHAMPIONS`` /
    ``_BLOCKED_BY_SLUGS``) is a SEPARATE repo and not a runtime or test
    dependency of crucible-dashboard, so a live cross-repo import is not
    feasible in this repo's CI environment. The sets below are a hardcoded
    snapshot of that module's ground truth as verified 2026-08-04
    (``VALID_CHAMPIONS`` L233, ``_BLOCKED_BY_SLUGS`` L250-276 as of that
    read). This test catches the DASHBOARD's constants drifting from this
    recorded snapshot; it does NOT automatically detect the backtester's
    vocabulary changing again — re-verify and update this snapshot whenever
    champion_promotion.py's VALID_CHAMPIONS or _BLOCKED_BY_SLUGS changes.
    """

    # optimizer/champion_promotion.py::VALID_CHAMPIONS
    _BACKTESTER_VALID_CHAMPIONS = {"scanner_predictor_direct", "thinktank_coverage"}

    # optimizer/champion_promotion.py::_BLOCKED_BY_SLUGS — current
    # winner-take-all vocabulary (I2518/I2544/I2998).
    _BACKTESTER_CURRENT_BLOCKED_BY_SLUGS = {
        "no_valid_scanner_predictor_direct_selections",
        "no_valid_thinktank_coverage_selections",
        "scanner_predictor_direct_counterfactual_unavailable",
        "thinktank_coverage_not_in_leaderboard",
        "thinktank_coverage_no_resolved_outcomes",
        "leaderboard_unavailable",
        "leaderboard_stale_gt_8d",
        "arm_score_unavailable",
        "feed_producer_dead",
        "frozen",
        "unclassified_error",
    }

    # Retired vocabularies _BLOCKED_BY_SLUGS keeps read-tolerated for
    # historical audit records (pre-I2518 HAC/hysteresis/cooldown engine, and
    # the same-day-superseded pre-I2544 exact-date leaderboard read).
    _BACKTESTER_RETIRED_BLOCKED_BY_SLUGS = {
        "insufficient_matured_cohorts",
        "cooldown_active",
        "not_significant_hac_adjusted",
        "hysteresis_not_satisfied",
        "leaderboard_stale",
    }

    def test_champion_arms_match_backtester_valid_champions(self):
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        champion_arms = set(_extract_literal(src, "_CHAMPION_ARMS"))
        assert champion_arms == self._BACKTESTER_VALID_CHAMPIONS, (
            "_CHAMPION_ARMS has drifted from champion_promotion.py's "
            "VALID_CHAMPIONS — update both the dashboard constant and this "
            "test's snapshot"
        )

    def test_champion_arm_labels_cover_every_champion_arm(self):
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        champion_arms = set(_extract_literal(src, "_CHAMPION_ARMS"))
        labels = _extract_literal(src, "_CHAMPION_ARM_LABELS")
        missing = champion_arms - set(labels)
        assert not missing, f"_CHAMPION_ARM_LABELS missing labels for: {sorted(missing)}"

    def test_blocked_by_labels_cover_every_current_and_retired_slug(self):
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        labels = set(_extract_literal(src, "_BLOCKED_BY_LABELS"))
        all_slugs = (
            self._BACKTESTER_CURRENT_BLOCKED_BY_SLUGS
            | self._BACKTESTER_RETIRED_BLOCKED_BY_SLUGS
        )
        missing = all_slugs - labels
        assert not missing, f"_BLOCKED_BY_LABELS missing labels for: {sorted(missing)}"

    def test_producer_cohort_prefixes_include_thinktank_coverage(self):
        """registry.py registers thinktank_coverage as a scored challenger
        arm (config-I4983); its shadow cohort prefix must be tracked here
        for maturity accounting even though build=None means the weekly
        producer run never builds it."""
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        prefixes = _extract_literal(src, "_PRODUCER_COHORT_PREFIXES")
        assert prefixes.get("thinktank_coverage") == "signals_shadow/thinktank_coverage/"
