"""Tests for the live Promotion Record page (alpha-engine-config-I10218).

Three things are pinned here, and each maps to a binding constraint on this
surface rather than to an implementation detail:

  * **Nav order** — the Promotion Record page is the FIRST entry in
    ``live/app.py``'s ``st.navigation`` and carries ``default=True``, with
    Live Portfolio second (Brian's ruling 2026-09-08, option (e)). A
    regression here silently restores the returns-first information
    architecture the ruling removed.
  * **Verdict-with-reason** — ``architecture.d/069`` point 3: no grader
    verdict may render without its reason. Tested at the chokepoint
    (``verdict_line``) and at every public verdict/row builder, including
    records that carry no reason at all.
  * **Awaiting data** — ``principles.md`` §2.7: a component emitting nothing
    is unobserved, not healthy. The missing producers are NAMED.

Plus the two constraints that are properties of the whole page: nothing
benchmark-relative, and no ops content reachable from this surface. Both are
asserted against the page source, because both are about what the file may
contain at all.

The pure logic lives in ``live/promotion_record.py`` (no streamlit, no
boto3, no S3), so most of this runs as ordinary unit tests.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIVE = _REPO_ROOT / "live"
for _p in (str(_REPO_ROOT), str(_LIVE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from promotion_record import (  # noqa: E402
    BLOCKED_BY_LABELS,
    CHAMPION_COMPONENT,
    MODEL_ZOO_CANDIDATE_COLUMNS,
    MODEL_ZOO_COMPONENT,
    NO_REASON_RECORDED,
    OUTCOME_LABELS,
    arm_label,
    champion_evidence_notes,
    champion_history_rows,
    champion_reason,
    champion_score_rows,
    champion_verdict,
    fmt_score,
    missing_components,
    model_zoo_candidate_rows,
    model_zoo_estimator_note,
    model_zoo_history_rows,
    model_zoo_verdict,
    staleness_note,
    verdict_line,
)

_APP = _LIVE / "app.py"
_PAGE = _LIVE / "pages" / "promotion_decisions.py"
_PAGE_BASENAME = "promotion_decisions.py"


# ---------------------------------------------------------------------------
# Fixtures — shaped after producer_champion_audit.schema.json v2 and the
# model-zoo leaderboard the weekly rotation writes.
# ---------------------------------------------------------------------------


def _audit(**overrides):
    base = {
        "schema_version": 2,
        "date": "2026-09-05",
        "outcome": "unchanged_winner_already_champion",
        "champion_before": "scanner_predictor_direct",
        "champion_after": "scanner_predictor_direct",
        "challenger": "thinktank_coverage",
        "champion_score": 0.0142,
        "challenger_score": 0.0091,
        "blocked_by": None,
        "freeze": False,
    }
    base.update(overrides)
    return base


def _leaderboard(**overrides):
    base = {
        "date": "2026-09-05",
        "mode": "cutover",
        "promotion_baseline_ic": 0.0210,
        "promotion_baseline_source": "champion_arch_fresh_retrain",
        "margin": 0.0015,
        "winner_version_id": "v_2026_09_05_a",
        "promoted": None,
        "candidates": [
            {
                "spec_id": "lgbm_base",
                "version_id": "v_2026_09_05_a",
                "forward_days": 21,
                "cpcv_mean_ic": 0.0223,
                "passes_gate": True,
                "eligible": True,
                "reason": "cleared champion-arch by 0.0013 (< required margin)",
            },
            {
                "spec_id": "ridge_wide",
                "version_id": "v_2026_09_05_b",
                "forward_days": 21,
                "cpcv_mean_ic": 0.0104,
                "passes_gate": False,
                "eligible": False,
                "reason": "below the registry bar",
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Nav order (ruling option (e))
# ---------------------------------------------------------------------------


class TestNavOrder:
    """``st.navigation`` order is the ruling, expressed as code."""

    @staticmethod
    def _nav_pages() -> list[tuple[str, str | None, bool]]:
        """(page basename, title, is_default) for each st.Page in the
        st.navigation list, in source order — parsed from the AST rather
        than string-matched, so a reordering cannot pass by coincidence."""
        tree = ast.parse(_APP.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "navigation"):
                continue
            pages = []
            for element in node.args[0].elts:
                assert isinstance(element, ast.Call)
                basename = element.args[0].args[-1].value
                title = None
                is_default = False
                for kw in element.keywords:
                    if kw.arg == "title":
                        title = kw.value.value
                    if kw.arg == "default":
                        is_default = bool(kw.value.value)
                pages.append((basename, title, is_default))
            return pages
        raise AssertionError("no st.navigation call found in live/app.py")

    def test_promotion_record_is_first_and_default(self):
        pages = self._nav_pages()
        assert pages, "live/app.py registers no pages"
        basename, title, is_default = pages[0]
        assert basename == _PAGE_BASENAME, (
            f"the Promotion Record page must be the FIRST nav entry "
            f"(ruling alpha-engine-config-I10218 option (e)); first is "
            f"{basename!r}"
        )
        assert is_default is True, "the first nav entry must carry default=True"
        assert title == "Promotion Record"

    def test_live_portfolio_is_second_and_not_default(self):
        pages = self._nav_pages()
        assert len(pages) >= 2
        basename, title, is_default = pages[1]
        assert basename == "holdings_and_trades.py"
        assert title == "Live Portfolio"
        assert is_default is False, (
            "Live Portfolio was demoted to second; two default pages, or a "
            "default on the portfolio, restores the framing the ruling removed"
        )

    def test_exactly_one_default_page(self):
        defaults = [p for p in self._nav_pages() if p[2]]
        assert len(defaults) == 1, f"expected one default page, got {defaults}"

    def test_holdings_docstring_no_longer_claims_default_landing(self):
        src = (_LIVE / "pages" / "holdings_and_trades.py").read_text()
        doc = ast.get_docstring(ast.parse(src)) or ""
        assert "Default landing page" not in doc, (
            "holdings_and_trades.py's docstring claims a role it no longer "
            "has — docs self-correct on contradiction"
        )
        assert "SECOND" in doc or "second" in doc


# ---------------------------------------------------------------------------
# Verdict-with-reason (architecture.d/069 point 3)
# ---------------------------------------------------------------------------


class TestVerdictCarriesItsReason:
    def test_verdict_line_substitutes_an_explicit_sentence_for_no_reason(self):
        for empty in (None, "", "   "):
            out = verdict_line("Held", empty)
            assert NO_REASON_RECORDED in out
            assert out != "Held"

    def test_verdict_line_keeps_a_real_reason(self):
        assert verdict_line("Promoted", "it won") == "Promoted — it won"

    @pytest.mark.parametrize("outcome", sorted(OUTCOME_LABELS))
    def test_every_outcome_renders_with_a_reason(self, outcome):
        # A record carrying ONLY its outcome — the worst case the producer
        # can legally emit — must still explain itself.
        out = champion_verdict({"outcome": outcome})
        assert " — " in out, f"{outcome} rendered without a reason: {out!r}"
        assert out.split(" — ", 1)[1].strip()

    def test_absent_audit_still_carries_a_reason(self):
        for empty in (None, {}, [], "nonsense"):
            out = champion_verdict(empty)  # type: ignore[arg-type]
            assert NO_REASON_RECORDED in out

    def test_blocked_by_slugs_become_sentences(self):
        audit = _audit(
            outcome="no_contest",
            blocked_by=["leaderboard_stale_gt_8d", "arm_score_unavailable"],
        )
        out = champion_verdict(audit)
        assert "Held — no contest" in out
        assert BLOCKED_BY_LABELS["leaderboard_stale_gt_8d"] in out
        assert BLOCKED_BY_LABELS["arm_score_unavailable"] in out

    def test_unmapped_blocked_by_slug_is_not_dropped(self):
        audit = _audit(outcome="no_contest", blocked_by=["a_brand_new_slug"])
        assert "a_brand_new_slug" in champion_verdict(audit)

    def test_shadow_only_hold_names_the_policy_and_the_arm(self):
        audit = _audit(
            outcome="held_shadow_only",
            blocked_by=["shadow_only_arm"],
            counterfactual_winner="thinktank_coverage",
        )
        out = champion_verdict(audit)
        assert "not eligible for promotion" in out
        assert arm_label("thinktank_coverage") in out
        # A policy hold is not a fault and must not be worded as one.
        assert "failed" not in out.lower()
        assert "broken" not in out.lower()

    def test_defended_incumbency_reason_states_both_scores(self):
        reason = champion_reason(_audit())
        assert "0.0142" in reason and "0.0091" in reason

    def test_error_outcome_uses_its_own_detail(self):
        out = champion_verdict(_audit(outcome="error", detail="scoring raised"))
        assert "scoring raised" in out

    def test_history_rows_each_carry_a_why(self):
        rows = champion_history_rows([
            _audit(),
            {"date": "2026-08-29", "outcome": "no_contest"},
            {},
        ])
        assert len(rows) == 2
        for row in rows:
            assert row["Why"].strip(), f"row without a reason: {row}"

    def test_model_zoo_verdict_carries_a_reason(self):
        for lb in (
            _leaderboard(),
            _leaderboard(mode="observe"),
            _leaderboard(promoted="v_2026_09_05_a", promoted_kind="challenger"),
            {},
            None,
        ):
            out = model_zoo_verdict(lb)
            assert " — " in out and out.split(" — ", 1)[1].strip()

    def test_model_zoo_candidate_without_a_reason_gets_the_explicit_sentence(self):
        lb = _leaderboard(candidates=[
            {"spec_id": "x", "version_id": "v1", "cpcv_mean_ic": 0.01,
             "passes_gate": False},
        ])
        row = model_zoo_candidate_rows(lb)[0]
        assert row["Passes gate"] == "no"
        assert row["Why"] == NO_REASON_RECORDED

    def test_model_zoo_candidate_rows_keep_gate_verdict_and_reason_together(self):
        rows = model_zoo_candidate_rows(_leaderboard())
        assert len(rows) == 2
        for row in rows:
            assert row["Passes gate"] in ("yes", "no")
            assert row["Why"].strip()

    def test_winner_flag_marks_only_the_winner(self):
        rows = model_zoo_candidate_rows(_leaderboard())
        assert [r["Winner"] for r in rows] == ["yes", "no"]

    def test_no_winner_id_marks_nobody(self):
        rows = model_zoo_candidate_rows(_leaderboard(winner_version_id=None))
        assert all(r["Winner"] == "no" for r in rows)


# ---------------------------------------------------------------------------
# Awaiting data (principles §2.7)
# ---------------------------------------------------------------------------


class TestAwaitingDataNamesTheMissingComponents:
    def test_both_absent_names_both(self):
        assert missing_components(None, None) == [
            CHAMPION_COMPONENT, MODEL_ZOO_COMPONENT
        ]

    def test_empty_dict_counts_as_absent(self):
        assert missing_components({}, {}) == [CHAMPION_COMPONENT, MODEL_ZOO_COMPONENT]

    def test_one_absent_names_only_that_one(self):
        assert missing_components(_audit(), None) == [MODEL_ZOO_COMPONENT]
        assert missing_components(None, _leaderboard()) == [CHAMPION_COMPONENT]

    def test_both_present_names_nothing(self):
        assert missing_components(_audit(), _leaderboard()) == []

    def test_component_names_are_readable_not_slugs(self):
        for name in (CHAMPION_COMPONENT, MODEL_ZOO_COMPONENT):
            assert " " in name and "/" not in name

    def test_page_renders_the_missing_names_and_never_as_success(self):
        src = _PAGE.read_text()
        assert "missing_components(" in src
        assert "st.warning(" in src, (
            "the awaiting-data state must not render as an info/success box"
        )
        # No-data is never green: st.success must not appear at all.
        assert "st.success(" not in src

    def test_absent_score_renders_absent_not_zero(self):
        for value in (None, "", [], True, False):
            assert fmt_score(value) == "—"
        assert fmt_score(0.0) == "0.0000"

    def test_score_rows_show_an_em_dash_for_an_unscored_arm(self):
        audit = _audit(outcome="no_contest", challenger_score=None,
                       blocked_by=["arm_score_unavailable"])
        rows = champion_score_rows(audit)
        assert rows[1]["Weekly gate score"] == "—"

    def test_evidence_notes_are_empty_rather_than_invented(self):
        assert champion_evidence_notes(_audit()) == []
        notes = champion_evidence_notes(_audit(arm_confidence={
            "scanner_predictor_direct": "ok", "thinktank_coverage": "thin",
        }))
        assert any("thin" in n for n in notes)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


class TestStaleness:
    def test_fresh_record_produces_no_note(self):
        assert staleness_note(
            CHAMPION_COMPONENT, "2026-09-05", "2026-09-05", today=date(2026, 9, 8)
        ) is None

    def test_stale_record_names_the_component_and_the_age(self):
        note = staleness_note(
            CHAMPION_COMPONENT, "2026-07-01", "2026-07-01", today=date(2026, 9, 8)
        )
        assert note and CHAMPION_COMPONENT in note and "69 days" in note

    def test_measured_write_date_wins_over_the_payloads_own_date(self):
        # The payload claims today; the measured write date is months old.
        # Ageing the payload would read a dead producer as current.
        note = staleness_note(
            MODEL_ZOO_COMPONENT, "2026-06-01", "2026-09-08", today=date(2026, 9, 8)
        )
        assert note and "measured write date" in note

    def test_falls_back_to_the_payload_date_and_says_so(self):
        note = staleness_note(
            MODEL_ZOO_COMPONENT, None, "2026-06-01", today=date(2026, 9, 8)
        )
        assert note and "self-reported" in note

    def test_unknown_vintage_is_stated_not_assumed_fresh(self):
        note = staleness_note(MODEL_ZOO_COMPONENT, None, None, today=date(2026, 9, 8))
        assert note and "unknown vintage" in note


# ---------------------------------------------------------------------------
# Nothing benchmark-relative (positioning §4) · no ops content (069 point 4)
# ---------------------------------------------------------------------------


class TestSurfaceExclusions:
    """These are properties of the page, so they are asserted over its source
    and over the field allowlist — not over one rendered example."""

    #: Benchmark-relative vocabulary. crucible-dashboard-PR834 removed this
    #: content from /live under ruling alpha-engine-config-I10215; a new page
    #: that reintroduces it under another name defeats that change.
    FORBIDDEN_BENCHMARK_TERMS = (
        "spy", "s&p", "sharpe", "information ratio", "hit rate", "hit_rate",
        "win rate", "win_rate", "benchmark", "outperform", "excess return",
        "cumulative_alpha", "alpha_bps", "nav",
    )

    #: Ops surfaces architecture.d/069 point 4 keeps structurally unreachable.
    FORBIDDEN_OPS_TERMS = (
        "groom", "sf_watch", "sfwatch", "ci-watch", "ci_watch", "box_health",
        "box-health", "freshness_monitor", "freshness-monitor",
        "agent_telemetry", "agent-telemetry", "decision queue",
        "decision_queue", "step function", "stepfunctions", "systemd",
        "cloudwatch", "pagerduty", "chasing_noise", "selection_pbo",
        "pbo_pass", "runbook",
    )

    @staticmethod
    def _prose(path: Path) -> str:
        """Everything the module can put in front of a reader, lowercased:
        every string literal that is not a docstring, plus every attribute
        and identifier name.

        Docstrings and comments are excluded deliberately — this module's own
        docstrings NAME the excluded surfaces in order to document why they
        are excluded, and a guard that cannot tell "renders SPY" from
        "must never render SPY" would force the rule to go unwritten to stay
        passing. Identifiers stay IN scope: a leaked ops field name reaching
        a dataframe column is as much a leak as leaked prose.
        """
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        parts: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    parts.append(node.value)
            elif isinstance(node, ast.Attribute):
                parts.append(node.attr)
            elif isinstance(node, ast.Name):
                parts.append(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                parts.append(node.name)
        return " ".join(parts).lower()

    @pytest.mark.parametrize("term", FORBIDDEN_BENCHMARK_TERMS)
    def test_page_carries_no_benchmark_relative_content(self, term):
        text = self._prose(_PAGE)
        # Word-ish boundary so "analysis" doesn't match "nav" etc.
        assert not re.search(rf"(?<![a-z_]){re.escape(term)}(?![a-z_])", text), (
            f"{_PAGE.name} mentions {term!r} — /live's promotion record is "
            f"not benchmark-relative (positioning §4)"
        )

    @pytest.mark.parametrize("term", FORBIDDEN_BENCHMARK_TERMS)
    def test_pure_module_carries_no_benchmark_relative_content(self, term):
        text = self._prose(_LIVE / "promotion_record.py")
        assert not re.search(rf"(?<![a-z_]){re.escape(term)}(?![a-z_])", text), (
            f"promotion_record.py mentions {term!r}"
        )

    @pytest.mark.parametrize("term", FORBIDDEN_OPS_TERMS)
    def test_no_ops_content_on_the_page(self, term):
        assert term not in self._prose(_PAGE), (
            f"{_PAGE.name} mentions {term!r} — no ops tile is reachable from "
            f"the external surface (architecture.d/069 point 4)"
        )

    @pytest.mark.parametrize("term", FORBIDDEN_OPS_TERMS)
    def test_no_ops_content_in_the_pure_module(self, term):
        assert term not in self._prose(_LIVE / "promotion_record.py")

    def test_candidate_columns_are_an_allowlist_of_gate_fields(self):
        # An upstream field addition must not be able to reach this surface
        # by default. Pin the set so widening it is a deliberate edit.
        assert set(MODEL_ZOO_CANDIDATE_COLUMNS) == {
            "spec_id", "version_id", "forward_days", "cpcv_mean_ic",
            "passes_gate", "eligible", "reason",
        }

    def test_candidate_rows_drop_an_unlisted_upstream_field(self):
        lb = _leaderboard()
        lb["candidates"][0]["selection_pbo"] = 0.42
        lb["candidates"][0]["box_health"] = "degraded"
        row = model_zoo_candidate_rows(lb)[0]
        assert "selection_pbo" not in " ".join(map(str, row.keys())).lower()
        assert "degraded" not in " ".join(map(str, row.values())).lower()

    def test_history_rows_carry_no_guardrail_telemetry(self):
        rows = model_zoo_history_rows([
            {"date": "2026-09-05", "mode": "cutover", "baseline_ic": 0.02,
             "winner_ic": 0.022, "margin": 0.001, "n_candidates": 2,
             "n_eligible": 1, "promoted": None, "pbo": 0.4,
             "chasing_noise": True},
        ])
        joined = " ".join(f"{k}{v}" for k, v in rows[0].items()).lower()
        assert "pbo" not in joined and "chasing" not in joined

    def test_estimator_note_names_the_estimator_and_the_baseline(self):
        note = model_zoo_estimator_note(_leaderboard())
        assert "cross-validation" in note.lower()
        assert "out of sample" in note.lower()
        assert "champion_arch_fresh_retrain" in note
        assert "Candidates scored this cycle: 2" in note

    def test_estimator_note_does_not_invent_a_baseline(self):
        note = model_zoo_estimator_note(_leaderboard(promotion_baseline_source=None))
        assert "not recorded" in note

    def test_page_applies_no_status_colour_to_an_outcome(self):
        # st.metric/st.dataframe carry no verdict colour; st.success would.
        # st.error/st.info on a scored outcome is the same failure mode.
        src = _PAGE.read_text()
        for banned in ("st.success(", "st.error(", "delta_color"):
            assert banned not in src, (
                f"{banned} on this surface reads a measured result as a "
                f"malfunction (architecture.d/069 point 1)"
            )

    def test_global_disclaimer_survives_and_is_not_duplicated(self):
        app_src = _APP.read_text()
        assert app_src.count("**Paper trading**") == 1
        assert "**Paper trading**" not in _PAGE.read_text()


# ---------------------------------------------------------------------------
# Loader wiring
# ---------------------------------------------------------------------------


class TestLoadersArePortedIntoTheLiveTree:
    """The page must reach its data through ``live/loaders/s3_loader.py``.
    ``live/`` shadows the name ``loaders`` at runtime, so a page importing
    the console loader would raise ImportError on the PUBLIC site."""

    REQUIRED = (
        "load_champion_pointer",
        "list_champion_audit_dates",
        "load_champion_audit",
        "load_champion_audit_latest",
        "champion_audit_last_modified",
        "load_model_zoo_leaderboard",
        "list_model_zoo_leaderboard_dates",
        "load_model_zoo_history",
        "model_zoo_leaderboard_last_modified",
    )

    @pytest.mark.parametrize("name", REQUIRED)
    def test_live_loader_defines_the_function(self, name):
        src = (_LIVE / "loaders" / "s3_loader.py").read_text()
        assert re.search(rf"^def {name}\(", src, re.M), (
            f"{name} is missing from live/loaders/s3_loader.py"
        )

    def test_page_imports_only_from_the_live_loader(self):
        tree = ast.parse(_PAGE.read_text())
        modules = {
            n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        }
        assert "loaders.s3_loader" in modules
        for mod in modules:
            assert mod is None or not mod.startswith("views"), mod

    def test_pure_module_imports_no_io(self):
        # promotion_record.py must stay unit-testable: no streamlit, boto3,
        # pandas or S3 anywhere in it.
        src = (_LIVE / "promotion_record.py").read_text()
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & {"streamlit", "boto3", "pandas", "loaders"}), imported


def test_page_file_exists_and_parses():
    assert _PAGE.exists()
    ast.parse(_PAGE.read_text())
    assert os.path.basename(str(_PAGE)) == _PAGE_BASENAME
