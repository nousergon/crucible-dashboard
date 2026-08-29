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

import pytest

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
    read; re-verified 2026-08-17 for the alpha-engine-config-I7549 evidence
    -admissibility slugs). This test catches the DASHBOARD's constants drifting from this
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
        # Evidence-confidence verdicts (alpha-engine-config-I7549,
        # 2026-08-17) — re-verified against champion_promotion.py
        # _BLOCKED_BY_SLUGS at crucible-backtester 1070a4e (#688, the
        # challenger-side half + leaderboard_horizon_mismatch) plus the
        # champion-side half in its open follow-up PR.
        "thinktank_coverage_thin_evidence",
        "thinktank_coverage_confidence_unknown",
        "leaderboard_horizon_mismatch",
        "scanner_predictor_direct_thin_evidence",
        "scanner_predictor_direct_confidence_unknown",
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


class TestEvidenceRendering:
    """alpha-engine-config-I7549 — a week the gate declined to decide must be
    readable as such, not as a defended incumbency.

    The backtester's weekly audit record now carries an ``evidence`` block
    naming, per arm, the confidence that admitted or refused its score. This
    page is the surface where "the challenger lost" and "we could not tell"
    have to be distinguishable.
    """

    def _view(self):
        """The page's module-level body renders the Streamlit page on import,
        so import the DEFINITIONS only: parse the source and execute just the
        imports, constants and function defs. Same source of truth as the
        _extract_literal tests above, exercised as real code rather than as
        text."""
        import ast
        import types
        path = REPO_ROOT / "views" / "46_Experiments.py"
        src = path.read_text()
        tree = ast.parse(src)
        def _is_constant_assign(node):
            # Module-level constants only (_LEADING_UNDERSCORE or UPPER_CASE)
            # — the page body's own `producer_tab, ... = st.tabs(...)` is also
            # an Assign, and executing it would render the page.
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                return False
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            return all(
                isinstance(t, ast.Name)
                and (t.id.startswith("_") or t.id.isupper())
                for t in targets
            )

        tree.body = [
            n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))
            or _is_constant_assign(n)
        ]
        mod = types.ModuleType("_experiments_view_defs")
        mod.__file__ = str(path)
        exec(compile(tree, str(path), "exec"), mod.__dict__)
        return mod

    def test_thin_evidence_renders_as_its_own_phrase(self):
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        labels = _extract_literal(src, "_BLOCKED_BY_LABELS")
        thin = labels["thinktank_coverage_thin_evidence"]
        lost = labels["thinktank_coverage_no_resolved_outcomes"]
        assert thin != lost
        assert "thin" in thin.lower()

    def test_confidence_column_is_rendered_beside_the_mean(self):
        """I7542's closes-when: no consumer presents a thin arm's mean as a
        comparison without the status alongside it."""
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        columns = _extract_literal(src, "_METRIC_COLUMNS")
        assert "confidence" in columns
        assert "n_dates_scored" in columns

    def test_evidence_label_names_each_arms_confidence(self):
        mod = self._view()
        audit = {
            "outcome": "no_contest",
            "blocked_by": ["thinktank_coverage_thin_evidence"],
            # The shape crucible-backtester actually writes (#688): a flat
            # arm -> verdict map of strings.
            "arm_confidence": {
                "scanner_predictor_direct": "ok",
                "thinktank_coverage": "thin",
            },
        }
        label = mod._evidence_label(audit)
        assert "thin" in label
        assert "ok" in label
        assert "Think Tank" in label

    def test_pre_champion_side_verdict_is_read_tolerated(self):
        """Records written between #688 and the champion-side half carry
        `not_leaderboard_scored` for that arm — rendered as-is, not dropped."""
        mod = self._view()
        label = mod._evidence_label({
            "arm_confidence": {
                "scanner_predictor_direct": "not_leaderboard_scored",
                "thinktank_coverage": "ok",
            },
        })
        assert "not_leaderboard_scored" in label

    def test_pre_i7549_audit_record_renders_empty_not_ok(self):
        """A record with no evidence block makes no claim about evidence —
        rendering it as "ok" would be the console asserting something the
        artifact never said."""
        mod = self._view()
        assert mod._evidence_label({"outcome": "no_contest"}) == ""
        assert mod._evidence_label(
            {"outcome": "no_contest", "arm_confidence": None},
        ) == ""


class TestShadowOnlyHoldRendering(TestEvidenceRendering):
    """alpha-engine-config-I2515 (Brian's 2026-08-20 shadow-only ruling) /
    I7836 — a week held because policy forbids promoting a shadow-only arm
    must read as neither a gate failure nor an ordinary defended incumbency,
    and the counterfactual winner (the entire point of shadow mode) must be
    visible on the same row.

    Fixture note: no live `held_shadow_only` audit record exists in S3 yet
    (crucible-backtester-PR712 merged 2026-08-20T19:34:11Z; the gate next
    fires the coming Saturday). The synthetic record below matches the
    Closes-when in alpha-engine-config-I7836 and the v2 contract shape added
    by nousergon-lib-PR336 (`outcome`, `blocked_by`, `counterfactual_winner`).
    Inherits ``_view`` from TestEvidenceRendering rather than duplicating it.
    """

    _HELD_SHADOW_ONLY_AUDIT = {
        "outcome": "held_shadow_only",
        "blocked_by": ["shadow_only_arm"],
        "champion_before": "scanner_predictor_direct",
        "champion_after": "scanner_predictor_direct",
        "counterfactual_winner": "thinktank_coverage",
    }

    def test_held_shadow_only_label_names_the_hold_not_a_failure(self):
        mod = self._view()
        label = mod._gate_state_label(self._HELD_SHADOW_ONLY_AUDIT)
        assert "held" in label.lower()
        assert "shadow" in label.lower()
        for word in ("fail", "block", "error", "degrad", "outage"):
            assert word not in label.lower(), (
                f"'{word}' in held_shadow_only label reads as a defect; "
                "the gate worked and policy forbade the promotion"
            )

    def test_held_shadow_only_label_distinguishes_from_no_contest_and_defended(self):
        mod = self._view()
        held = mod._gate_state_label(self._HELD_SHADOW_ONLY_AUDIT)
        no_contest = mod._gate_state_label({
            "outcome": "no_contest",
            "blocked_by": ["thinktank_coverage_no_resolved_outcomes"],
        })
        defended = mod._gate_state_label({
            "outcome": "no_contest",
            "blocked_by": ["arm_score_unavailable"],
        })
        assert held != no_contest
        assert held != defended

    def test_held_shadow_only_label_surfaces_counterfactual_winner(self):
        mod = self._view()
        label = mod._gate_state_label(self._HELD_SHADOW_ONLY_AUDIT)
        assert "Think Tank" in label

    def test_shadow_only_arm_blocked_by_label_present(self):
        """Deliverable 1 of I7836: the raw blocked_by slug also gets a label,
        independent of the outcome branch — covers any raw blocked_by read
        and keeps TestBlockedBySlugContractParity's schema-enum sweep green."""
        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        labels = _extract_literal(src, "_BLOCKED_BY_LABELS")
        assert "shadow_only_arm" in labels
        assert "shadow" in labels["shadow_only_arm"].lower()

    def test_counterfactual_winner_helper_renders_when_present(self):
        mod = self._view()
        assert mod._counterfactual_winner_label(
            self._HELD_SHADOW_ONLY_AUDIT,
        ) == "Think Tank coverage (per-ticker theses)"

    def test_counterfactual_winner_helper_absent_safe(self):
        """No claim is made when the field is missing (e.g. every
        non-held_shadow_only outcome, and pre-I2515 historical records)."""
        mod = self._view()
        assert mod._counterfactual_winner_label({"outcome": "no_contest"}) == ""
        assert mod._counterfactual_winner_label(
            {"outcome": "promoted", "counterfactual_winner": None},
        ) == ""

    def test_champion_history_frame_includes_would_have_promoted_column(self):
        """The counterfactual is surfaced on the SAME ROW as the hold in the
        Promotion history table, not only inside the combined label string."""
        mod = self._view()
        with patch.object(mod, "load_champion_audit", return_value=self._HELD_SHADOW_ONLY_AUDIT):
            frame = mod._champion_history_frame(["2026-08-22"])
        assert "Would have promoted" in frame.columns
        assert frame.iloc[0]["Would have promoted"] == "Think Tank coverage (per-ticker theses)"


contracts = pytest.importorskip(
    "nousergon_lib.contracts",
    reason="needs nousergon-lib[contracts] (jsonschema) installed",
)


class TestBlockedBySlugContractParity:
    """Reads the ``producer_champion_audit`` contract from the INSTALLED
    ``nousergon_lib.contracts`` package (alpha-engine-config-I7605) rather
    than a hand-copied slug list (contrast ``TestChampionVocabularyParity``
    above, whose snapshot must be updated by hand on every backtester
    vocabulary change and says so in its own docstring), and rather than the
    prior sibling-checkout filesystem walk of crucible-backtester's working
    tree (alpha-engine-config-I7605's finding: that walk's verdict depended
    on which branch/state that checkout happened to be in on the machine
    running the suite, not on the published contract). This test needs no
    manual update: the next ``blocked_by`` slug crucible-backtester's
    optimizer/champion_promotion.py adds turns this RED the moment this
    repo's ``nousergon-lib`` pin picks up the updated contract, instead of
    silently degrading to raw-slug display on the Experiments page
    (``_gate_state_label``'s ``.get(b, b)`` fallback).

    crucible-backtester (the producer) reads the SAME published resource —
    see its ``tests/test_champion_promotion.py::AUDIT_SCHEMA`` — so producer
    and consumer can never independently drift the way a sibling-checkout
    walk allowed.
    """

    @staticmethod
    def _schema() -> dict:
        return contracts.load_schema("producer_champion_audit")

    def test_every_schema_blocked_by_slug_has_a_dashboard_label(self):
        schema = self._schema()
        variants = schema["properties"]["blocked_by"]["oneOf"]
        array_variant = next(v for v in variants if v.get("type") == "array")
        slugs = set(array_variant["items"]["enum"])

        src = (REPO_ROOT / "views" / "46_Experiments.py").read_text()
        labels = set(_extract_literal(src, "_BLOCKED_BY_LABELS"))

        missing = slugs - labels
        assert not missing, (
            f"_BLOCKED_BY_LABELS in views/46_Experiments.py is missing "
            f"human labels for blocked_by slug(s) present in the "
            f"producer_champion_audit contract: {sorted(missing)}. See "
            f"_BLOCKED_BY_LABELS in views/46_Experiments.py."
        )

    def test_arm_confidence_field_declared_in_schema(self):
        """Guards the assumption ``_evidence_label`` (46_Experiments.py,
        config-I7549) relies on: ``arm_confidence`` is a nullable object of
        arm -> string verdict. A shape change here (e.g. a nested structure)
        would make that renderer silently print something wrong instead of
        failing."""
        schema = self._schema()
        arm_confidence = schema["properties"].get("arm_confidence")
        assert arm_confidence is not None, (
            "producer_champion_audit contract no longer declares "
            "arm_confidence — views/46_Experiments.py::_evidence_label "
            "needs re-checking against the new shape."
        )
        assert "object" in arm_confidence["type"]


class TestSlugContract:
    """The weekly RESEARCH champion/challenger verdict email
    (``crucible-backtester optimizer/champion_digest.py::EXPERIMENTS_SLUG``)
    deep-links to ``…/experiments?date=YYYY-MM-DD``. Mirrors
    ``test_analysis_page.py::TestSlugContract``.

    Before this pin the page carried Streamlit's filename-derived default slug
    ("46_Experiments"), held by no test — so the ONLY console surface rendering
    the champion/challenger verdict had no stable address to link to, and a
    file rename would have moved it silently.
    """

    # Must equal the producer slug in crucible-backtester
    # optimizer/champion_digest.EXPERIMENTS_SLUG.
    EXPECTED_SLUG = "experiments"

    def test_app_pins_experiments_url_path(self):
        app_src = (Path(__file__).parent.parent / "app.py").read_text()
        assert f'url_path="{self.EXPECTED_SLUG}"' in app_src

    def test_the_slug_is_pinned_on_the_experiments_page_itself(self):
        """A repo-wide substring match would pass on ANY page pinning this
        slug. Assert the pin sits in the same ``st.Page`` block as the
        Experiments view file."""
        app_src = (Path(__file__).parent.parent / "app.py").read_text()
        block = re.search(
            r'st\.Page\(\s*"views/46_Experiments\.py".*?\)', app_src, re.S,
        )
        assert block is not None, "46_Experiments.py is no longer a standalone st.Page"
        assert f'url_path="{self.EXPECTED_SLUG}"' in block.group(0)

    def test_page_file_exists(self):
        assert (Path(__file__).parent.parent / "views" / "46_Experiments.py").exists()
