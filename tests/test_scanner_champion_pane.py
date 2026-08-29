"""Tests for the Scanner champion/challenger pane and its loaders
(alpha-engine-config-I9278).

Mirrors test_experiments_page.py: streamlit is mocked (cache_data →
passthrough) and the page module itself is NOT imported (its module-level
Streamlit calls need a live runtime) — page wiring is asserted against source
text instead.

Every test here was verified RED against the pre-fix tree
(champion-challenger-policy.md §7.4: a guard that cannot fail is worse than no
guard, because it reads as coverage). Two are named in the issue and get their
own classes:

* ``TestStalenessIsRenderedNotBlank`` — a ``latest.json`` older than 8 days
  resolves to a RED state carrying the age, never to an empty pane.
* ``TestSchemaVersionFirewall`` — a v1 record's numbers can never appear under
  a v2 field label.
"""

import ast
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules.setdefault("streamlit", mock_st)

from loaders import s3_loader, scanner_champion as sc  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "56_Scanner_Champion.py"
LOADER = REPO_ROOT / "loaders" / "scanner_champion.py"

NOW = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)

CUT_SLOT = next(s for s in sc.SLOTS if s.slot_id == "scanner_cut")
SPEC_SLOT = next(s for s in sc.SLOTS if s.slot_id == "scanner_spec")


def _v2_record(*, decided_on="2026-08-28", generated_at=None, **over):
    """A v2 scanner-cut decision record, shaped from
    crucible-research/contracts/scanner_cut_champion.schema.json."""
    record = {
        "schema_version": 2,
        "producer": "crucible-research/scoring/cut_promotion.py",
        "slot_id": "scanner_cut",
        "generated_at": generated_at or f"{decided_on}T21:14:03+00:00",
        "decided_on": decided_on,
        "decision": "hold",
        "champion": "arm_alpha",
        "champion_before": "arm_alpha",
        "reason": "one promotable arm; no comparison possible",
        "reason_code": "no_promotable_challenger",
        "decision_metric": "paired_weekly_net_log_return_vs_champion",
        "decision_cadence": "weekly",
        "decision_source": "research/cuts_weekly_ledger/ledger.parquet",
        "decision_column": "net_log_return",
        "last_promoted_on": None,
        "decision_earliest_on": "2026-09-25",
        "arms": {
            "arm_alpha": {
                "present": True,
                "is_champion": True,
                "n_weeks_scored": 1,
                "n_weeks_paired": 1,
                "mean_paired_log_return": 0.0,
                "chained_paired_log_return": 0.0,
                "se": None,
                "t_stat": None,
                "confidence": "insufficient",
            },
            "arm_beta": {
                "present": False,
                "is_champion": False,
                "n_weeks_scored": 0,
                "n_weeks_paired": 0,
                "mean_paired_log_return": None,
                "chained_paired_log_return": None,
                "se": None,
                "t_stat": None,
                "confidence": "insufficient",
            },
        },
        "ledger": {
            "key": "research/cuts_weekly_ledger/ledger.parquet",
            "ledger_version": 1,
            "present": True,
        },
    }
    record.update(over)
    return record


def _v1_record(*, decided_on="2026-08-21", **over):
    """A v1 record — the pre-2026-08-28 shape. Its arm numbers are a
    126-session forward-window mean and mean something DIFFERENT from v2's."""
    record = {
        "schema_version": 1,
        "producer": "crucible-research/scoring/cut_promotion.py",
        "slot_id": "scanner_cut",
        "generated_at": f"{decided_on}T21:02:11+00:00",
        "decided_on": decided_on,
        "decision": "hold",
        "champion": "arm_alpha",
        "champion_before": "arm_alpha",
        "reason": "decision horizon immature",
        "reason_code": "no_promotable_challenger",
        "horizon_days": 126,
        "primary_metric": "topn_alpha_vs_population_mean",
        "last_promoted_on": None,
        "decision_earliest_on": "2027-02-22",
        "arms": {
            "arm_alpha": {
                "present": True,
                "is_champion": True,
                "n_dates_scored": 2,
                "topn_alpha_vs_population_mean": 0.0123,
                "t_stat": 1.9,
                "horizon_days": 126,
                "confidence": "thin",
            },
        },
    }
    record.update(over)
    return record


def _view(slot, *, pointer, latest, dates, records, written_at=None, now=NOW):
    return sc.build_slot_view(
        slot,
        pointer=pointer,
        latest=latest,
        latest_written_at=written_at,
        dated_dates=dates,
        records=records,
        now=now,
    )


# ---------------------------------------------------------------------------


class TestStalenessIsRenderedNotBlank:
    """Issue deliverable 2 — the liveness CLAIM.

    `config/apply_audit/scanner_cut_champion/latest.json` older than 8 days
    renders RED with the age, never blank. Nothing read that key before this
    pane, and it exists for exactly this purpose."""

    def test_latest_older_than_eight_days_is_a_red_state_carrying_the_age(self):
        stale_on = (NOW - timedelta(days=23)).date().isoformat()
        claim = sc.liveness(
            CUT_SLOT,
            latest=_v2_record(
                decided_on=stale_on,
                generated_at=(NOW - timedelta(days=23)).isoformat(),
            ),
            dated_dates=[stale_on],
            latest_written_at=None,
            now=NOW,
        )
        assert claim.state == "MISSED", "a stale slot must not resolve to HEALTHY"
        assert claim.age_days == 23
        assert "23" in claim.headline, "the age must be ON the claim, not inferred"
        assert "STALE" in claim.headline.upper()
        assert str(sc.STALE_AFTER_DAYS) in claim.headline + claim.detail

    def test_stale_slot_is_red_in_the_json_projection_too(self):
        stale_on = (NOW - timedelta(days=30)).date().isoformat()
        record = _v2_record(
            decided_on=stale_on, generated_at=(NOW - timedelta(days=30)).isoformat()
        )
        view = _view(
            CUT_SLOT, pointer=record, latest=record, dates=[stale_on], records=[record]
        )
        assert view["liveness"]["state"] == "MISSED"
        assert view["liveness"]["age_days"] == 30
        assert view["liveness"]["stale_after_days"] == 8

    def test_exactly_at_the_bound_is_not_yet_stale(self):
        on_bound = (NOW - timedelta(days=8)).date().isoformat()
        claim = sc.liveness(
            CUT_SLOT,
            latest=_v2_record(
                decided_on=on_bound,
                generated_at=(NOW - timedelta(days=8)).isoformat(),
            ),
            dated_dates=[on_bound],
            latest_written_at=None,
            now=NOW,
        )
        assert claim.state == "HEALTHY"
        assert claim.age_days == 8

    def test_a_fraction_past_the_bound_is_already_stale(self):
        """The floor of the age is what is DISPLAYED; the comparison is on the
        exact span. Comparing on the floor would let a record 8.9 days old
        clear an 8-day bound."""
        claim = sc.liveness(
            CUT_SLOT,
            latest=_v2_record(
                decided_on="2026-08-20",
                generated_at=(NOW - timedelta(days=8, hours=22)).isoformat(),
            ),
            dated_dates=["2026-08-20"],
            latest_written_at=None,
            now=NOW,
        )
        assert claim.state == "MISSED"
        assert claim.age_days == 8

    def test_absent_latest_with_dated_history_is_unreported_not_healthy(self):
        claim = sc.liveness(
            CUT_SLOT,
            latest=None,
            dated_dates=["2026-08-21", "2026-08-28"],
            latest_written_at=None,
            now=NOW,
        )
        assert claim.state == "UNREPORTED"
        assert claim.age_days is None, "no as-of ⇒ NO age claim, not age zero"
        assert "latest.json" in claim.headline

    def test_unparseable_as_of_cannot_claim_freshness(self):
        claim = sc.liveness(
            CUT_SLOT,
            latest=_v2_record(generated_at="not-a-timestamp", decided_on="nonsense"),
            dated_dates=["2026-08-28"],
            latest_written_at=None,
            now=NOW,
        )
        assert claim.state == "UNREPORTED"
        assert claim.age_days is None

    def test_s3_last_modified_is_the_fallback_as_of(self):
        record = _v2_record(generated_at="", decided_on="")
        claim = sc.liveness(
            CUT_SLOT,
            latest=record,
            dated_dates=["2026-08-28"],
            latest_written_at=(NOW - timedelta(days=40)).isoformat(),
            now=NOW,
        )
        assert claim.state == "MISSED"
        assert claim.as_of_source == "s3.LastModified"
        assert claim.age_days == 40

    def test_board_defective_is_a_failed_state_even_when_fresh(self):
        record = _v2_record(reason_code="board_defective", defect="duplicate arm rows")
        claim = sc.liveness(
            CUT_SLOT,
            latest=record,
            dated_dates=["2026-08-28"],
            latest_written_at=None,
            now=NOW,
        )
        assert claim.state == "FAILED"

    def test_no_branch_reaches_healthy_from_absence(self):
        """The four absence states are distinct and none of them is HEALTHY."""
        absent = sc.liveness(
            CUT_SLOT, latest=None, dated_dates=[], latest_written_at=None, now=NOW
        )
        assert absent.state == "NEVER_RAN"
        assert "no records yet" in absent.headline.lower()
        assert absent.state != "HEALTHY"


class TestSchemaVersionFirewall:
    """A v1 record rendered under v2 field labels is a fabricated fact."""

    V2_ONLY_LABELS = {
        "Mean paired net log-return vs champion",
        "Weeks paired",
        "Weeks scored",
        "Chained paired log-return",
    }
    V1_ONLY_LABELS = {
        "Top-N alpha vs population (mean)",
        "Cohort dates scored",
    }

    def test_v1_arms_never_render_under_a_v2_label(self):
        frame, specs, version = sc.arm_table(_v1_record())
        assert version == 1
        assert self.V2_ONLY_LABELS.isdisjoint(set(frame.columns))
        assert self.V1_ONLY_LABELS.issubset(set(frame.columns))

    def test_v2_arms_never_render_under_a_v1_label(self):
        frame, specs, version = sc.arm_table(_v2_record())
        assert version == 2
        assert self.V1_ONLY_LABELS.isdisjoint(set(frame.columns))
        assert self.V2_ONLY_LABELS.issubset(set(frame.columns))

    def test_the_two_vocabularies_are_never_merged(self):
        v1_keys = {f.key for f in sc.arm_fields(1)}
        v2_keys = {f.key for f in sc.arm_fields(2)}
        # The DECISION numbers are disjoint. t_stat/confidence are shared
        # metadata about evidence quality and legitimately appear in both.
        assert "topn_alpha_vs_population_mean" in v1_keys - v2_keys
        assert "mean_paired_log_return" in v2_keys - v1_keys
        assert "n_weeks_paired" not in v1_keys
        assert "n_dates_scored" not in v2_keys

    def test_decision_basis_labels_name_their_version(self):
        v1 = dict((label, value) for label, value, _ in sc.basis_row(_v1_record()))
        v2 = dict((label, value) for label, value, _ in sc.basis_row(_v2_record()))
        assert "Decision metric (v1)" in v1 and "Decision metric (v2)" not in v1
        assert "Decision metric (v2)" in v2 and "Decision metric (v1)" not in v2

    def test_each_version_note_states_what_the_numbers_mean(self):
        assert "126" in sc.schema_version_note(1)
        assert "paired" in sc.schema_version_note(2)
        assert "weekly" in sc.schema_version_note(2)
        assert "NOT comparable" in sc.schema_version_note(1)

    def test_unknown_version_renders_raw_and_drops_nothing(self):
        record = _v2_record(schema_version=99)
        frame, specs, version = sc.arm_table(record)
        assert version == 99 and specs == ()
        assert "mean_paired_log_return" in frame.columns, "raw key, not a v2 label"
        assert "Mean paired net log-return vs champion" not in frame.columns
        assert "RAW" in sc.schema_version_note(99)

    def test_missing_version_is_not_assumed_to_be_the_latest(self):
        record = _v2_record()
        record.pop("schema_version")
        assert sc.record_schema_version(record) is None
        frame, specs, version = sc.arm_table(record)
        assert version is None and specs == ()
        assert self.V2_ONLY_LABELS.isdisjoint(set(frame.columns))

    def test_every_declared_field_carries_a_unit_and_a_legal_render_hint(self):
        vocabulary = {
            "value", "duration", "bytes", "ratio",
            "count", "timeseries", "link", "text",
        }
        specs = [
            *sc.arm_fields(1), *sc.arm_fields(2),
            *sc.basis_fields(1), *sc.basis_fields(2),
            *sc.LEDGER_DECLARED_COLUMNS,
        ]
        assert specs
        for spec in specs:
            assert spec.unit, f"{spec.key} declares no unit"
            assert spec.render in vocabulary, f"{spec.key} render hint outside §5.8"


class TestNullIsNotZero:
    def test_none_renders_as_an_em_dash(self):
        assert sc.fmt_optional(None) == "—"

    def test_zero_renders_as_zero(self):
        assert sc.fmt_optional(0.0) != "—"
        assert "0" in sc.fmt_optional(0.0)
        assert sc.fmt_optional(0) == "0"

    def test_nan_renders_as_an_em_dash(self):
        assert sc.fmt_optional(float("nan")) == "—"

    def test_an_arm_with_no_evidence_shows_a_dash_not_a_zero(self):
        frame, _, _ = sc.arm_table(_v2_record())
        row = frame.loc[frame["Arm"] == "arm_beta"].iloc[0]
        assert row["Mean paired net log-return vs champion"] == "—"
        champ = frame.loc[frame["Arm"] == "arm_alpha"].iloc[0]
        assert champ["Mean paired net log-return vs champion"] != "—"


class TestReasonCodeDisposition:
    @pytest.mark.parametrize(
        "code",
        ["weekly_ledger_missing", "insufficient_weeks", "no_promotable_challenger"],
    )
    def test_expected_steady_states_are_normal_not_warnings(self, code):
        assert sc.reason_code_disposition(code) == "normal"

    def test_board_defective_is_the_only_defect(self):
        assert sc.reason_code_disposition("board_defective") == "defect"
        defects = {
            c for c in (
                sc._NORMAL_REASON_CODES | sc._DEFECT_REASON_CODES
            ) if sc.reason_code_disposition(c) == "defect"
        }
        assert defects == {"board_defective"}

    def test_retired_v1_slugs_stay_readable(self):
        for code in ("board_missing", "insufficient_dates", "arm_row_missing"):
            assert sc.reason_code_disposition(code) == "normal"

    def test_an_unknown_slug_is_unrecognised_not_benign(self):
        assert sc.reason_code_disposition("something_new") == "unrecognised"
        assert sc.reason_code_disposition(None) == "unrecognised"

    def test_taxonomy_matches_the_producers_contract(self):
        """Parity against crucible-research's own schema, when the sibling
        checkout is present (it is not on a CI runner — skipped there rather
        than asserted against a copy that could drift silently)."""
        schema = (
            REPO_ROOT.parent
            / "crucible-research"
            / "contracts"
            / "scanner_cut_champion.schema.json"
        )
        if not schema.exists():
            pytest.skip("crucible-research sibling checkout not present")
        import json

        enum = json.loads(schema.read_text())["properties"]["reason_code"]["enum"]
        known = sc._NORMAL_REASON_CODES | sc._DEFECT_REASON_CODES
        missing = set(enum) - known
        assert not missing, f"reason_code slug(s) with no disposition here: {missing}"


class TestArmsComeFromTheArtifact:
    """crucible-dashboard-PR803's defect, not repeated: a stale arm-list
    constant in this repo. Two scanner-cut arms become promotable shortly that
    are not today; the pane picks them up because it never had a list."""

    LIVE_ARM_LITERALS = ("attractiveness_top_60", "tech_score_top_60")

    def test_no_cut_arm_name_is_hardcoded_in_the_loader_or_the_page(self):
        for path in (LOADER, PAGE):
            src = path.read_text()
            for literal in self.LIVE_ARM_LITERALS:
                assert literal not in src, (
                    f"{path.name} names the arm {literal!r} — resolve arms from the "
                    "artifact, never from a constant in this repo"
                )

    def test_arms_are_read_from_the_records_own_block(self):
        record = _v2_record()
        record["arms"]["arm_gamma"] = {
            "present": True, "is_champion": False,
            "n_weeks_scored": 3, "n_weeks_paired": 3,
            "mean_paired_log_return": 0.001, "chained_paired_log_return": 0.003,
            "t_stat": 0.4, "confidence": "thin",
        }
        frame, _, _ = sc.arm_table(record)
        assert set(frame["Arm"]) == {"arm_alpha", "arm_beta", "arm_gamma"}

    def test_ledger_arms_are_read_from_the_ledgers_own_column(self):
        frame = pd.DataFrame({"arm": ["a", "b", "a"], "week_start": ["w1"] * 3})
        assert sc.ledger_arms(frame) == ["a", "b"]
        assert sc.ledger_arms(None) == []


class TestSpecSlotOnboardedWithoutRecords:
    """Issue deliverable 3 — the SPEC slot renders as an explicit absence today
    and starts rendering records with no further dashboard change."""

    def test_spec_slot_is_bound_to_its_keys(self):
        assert SPEC_SLOT.pointer_key == "config/scanner_spec_champion.json"
        assert SPEC_SLOT.audit_prefix == "config/apply_audit/scanner_spec_champion/"
        assert "spec_promotion.py" in SPEC_SLOT.producer

    def test_absent_spec_records_render_as_never_ran_naming_the_keys(self):
        view = _view(SPEC_SLOT, pointer=None, latest=None, dates=[], records=[])
        assert view["liveness"]["state"] == "NEVER_RAN"
        assert "no records yet" in view["liveness"]["headline"].lower()
        assert SPEC_SLOT.pointer_key in view["liveness"]["detail"]
        assert SPEC_SLOT.audit_prefix in view["liveness"]["detail"]
        assert view["pointer"]["present"] is False
        assert view["decisions"] == []

    def test_spec_records_render_through_the_same_path_with_no_code_change(self):
        record = _v2_record(slot_id="scanner_spec")
        view = _view(
            SPEC_SLOT,
            pointer=record,
            latest=record,
            dates=["2026-08-28"],
            records=[record],
        )
        assert view["liveness"]["state"] == "HEALTHY"
        assert view["pointer"]["champion"] == "arm_alpha"
        assert view["decisions"][0]["decided_on"] == "2026-08-28"

    def test_both_slots_are_rendered_by_one_generic_loop(self):
        src = PAGE.read_text()
        assert "for slot in SLOTS" in src, (
            "the page must iterate SLOTS — a per-slot rendering branch is the "
            "per-module rendering path console-policy.md §2.6 forbids"
        )
        assert "scanner_spec" not in src.split('"""', 2)[2], (
            "no slot id keyed into the page body"
        )


class TestDecisionSeries:
    def test_series_is_newest_first_and_version_independent(self):
        frame = sc.decision_series([_v1_record(), _v2_record()])
        assert list(frame["decided_on"]) == ["2026-08-28", "2026-08-21"]
        assert list(frame["schema_version"]) == [2, 1]
        for column in ("champion_before", "decision", "reason_code", "last_promoted_on"):
            assert column in frame.columns

    def test_empty_history_still_has_its_columns(self):
        frame = sc.decision_series([])
        assert frame.empty
        assert "reason_code" in frame.columns

    def test_history_is_capped_at_eight_records(self):
        records = [
            _v2_record(decided_on=f"2026-0{1 + i // 28}-{1 + i % 28:02d}")
            for i in range(12)
        ]
        view = _view(
            CUT_SLOT,
            pointer=records[0],
            latest=records[0],
            dates=[r["decided_on"] for r in records],
            records=records,
            now=datetime(2026, 2, 5, tzinfo=timezone.utc),
        )
        assert len(view["decisions"]) == sc.DECISION_HISTORY_LIMIT == 8
        assert view["decision_count_listed"] == 12


class TestWeeklyLedgerView:
    def _frame(self):
        return pd.DataFrame(
            {
                "arm": ["a", "b"],
                "week_start": ["2026-08-21", "2026-08-21"],
                "week_end": ["2026-08-28", "2026-08-28"],
                "n_names": [60, 60],
                "turnover_frac": [0.24, 0.58],
                "gross_log_return": [0.004, 0.006],
                "net_log_return": [0.002985, None],
                "net_unavailable_reason": [None, "cost_model_inputs_missing"],
                "code_sha": ["abc", "abc"],
            }
        )

    def test_absent_object_and_empty_object_are_different_renders(self):
        absent, _ = sc.ledger_view(None)
        empty, _ = sc.ledger_view(pd.DataFrame(columns=["arm", "week_start"]))
        assert absent.empty and empty.empty
        assert list(absent.columns) != list(empty.columns)

    def test_null_net_is_never_filled_from_gross(self):
        view, _ = sc.ledger_view(self._frame())
        row = view.loc[view["Arm"] == "b"].iloc[0]
        assert pd.isna(row["Net"]) or row["Net"] is None
        assert row["Why net is absent"] == "cost_model_inputs_missing"
        assert row["Gross"] == 0.006

    def test_undeclared_columns_are_reported_never_dropped(self):
        _, undeclared = sc.ledger_view(self._frame())
        assert "code_sha" in undeclared

    def test_declared_columns_cover_the_issues_named_fields(self):
        declared = {s.key for s in sc.LEDGER_DECLARED_COLUMNS}
        assert {
            "gross_log_return", "net_log_return", "net_unavailable_reason",
            "turnover_frac", "n_names",
        } <= declared


class TestSlotLoaders:
    def test_pointer_and_audit_keys_are_addressed_exactly(self):
        with patch.object(s3_loader, "download_s3_json", return_value={"ok": 1}) as dl, \
             patch.object(s3_loader, "_research_bucket", return_value="bkt"):
            s3_loader.load_slot_champion_pointer(CUT_SLOT.pointer_key)
            s3_loader.load_slot_audit(CUT_SLOT.audit_prefix, "2026-08-28")
            s3_loader.load_slot_audit_latest(CUT_SLOT.audit_prefix)
        keys = [c.args[1] for c in dl.call_args_list]
        assert keys == [
            "config/scanner_cut_champion.json",
            "config/apply_audit/scanner_cut_champion/2026-08-28.json",
            "config/apply_audit/scanner_cut_champion/latest.json",
        ]

    def test_ledger_is_read_whole_as_one_object(self):
        buf = _parquet_bytes()
        with patch.object(s3_loader, "_s3_get_object", return_value=buf) as get, \
             patch.object(s3_loader, "_research_bucket", return_value="bkt"):
            frame = s3_loader.load_cuts_weekly_ledger()
        assert get.call_count == 1, "one GET of the whole series, never per-week keys"
        assert get.call_args.args[1] == "research/cuts_weekly_ledger/ledger.parquet"
        assert len(frame) == 2

    def test_absent_ledger_is_none_not_an_empty_frame(self):
        with patch.object(s3_loader, "_s3_get_object", return_value=None), \
             patch.object(s3_loader, "_research_bucket", return_value="bkt"):
            assert s3_loader.load_cuts_weekly_ledger() is None

    def test_head_object_failure_renders_as_absence_and_is_recorded(self):
        client = MagicMock()
        client.head_object.side_effect = RuntimeError("boom")
        with patch.object(s3_loader, "get_s3_client", return_value=client), \
             patch.object(s3_loader, "_research_bucket", return_value="bkt"), \
             patch.object(s3_loader, "_record_s3_error") as rec:
            assert s3_loader.load_slot_audit_latest_written_at(CUT_SLOT.audit_prefix) is None
        assert rec.called, "a failed head_object is recorded, never silently swallowed"


def _parquet_bytes() -> bytes:
    import io

    frame = pd.DataFrame({"arm": ["a", "b"], "week_start": ["2026-08-21"] * 2})
    buf = io.BytesIO()
    frame.to_parquet(buf, index=False)
    return buf.getvalue()


class TestPageWiring:
    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_nav_registers_the_pane_with_a_pinned_slug(self):
        src = (REPO_ROOT / "app.py").read_text()
        assert "views/56_Scanner_Champion.py" in src
        match = re.search(
            r'"views/56_Scanner_Champion\.py".*?url_path="([a-z-]+)"', src, re.S
        )
        assert match, "the pane must be a standalone st.Page with a pinned url_path"
        assert match.group(1) == "scanner-champion"

    def test_page_reads_every_artifact_family_the_issue_names(self):
        src = PAGE.read_text()
        for symbol in (
            "load_slot_view",
            "load_cuts_weekly_ledger",
            "load_slot_audit",
            "list_slot_audit_dates",
        ):
            assert symbol in src, f"page does not read {symbol}"

    def test_page_is_a_valid_module(self):
        ast.parse(PAGE.read_text())

    def test_page_does_not_conflate_the_two_champion_slots(self):
        """The backtester's executor selection-path switch is a DIFFERENT slot
        (champion-challenger-policy.md §2). The page may NAME it to say so;
        it must not READ it."""
        src = PAGE.read_text()
        for symbol in ("load_champion_pointer", "load_champion_audit", "_CHAMPION_ARMS"):
            assert symbol not in src


class TestMachineReadableProjection:
    """console-policy.md §3.8 and the issue's Closes-when."""

    def test_projection_carries_pointer_history_and_per_arm_evidence(self):
        record = _v2_record()
        view = _view(
            CUT_SLOT,
            pointer=record,
            latest=record,
            dates=["2026-08-21", "2026-08-28"],
            records=[record, _v1_record()],
        )
        assert view["schema_version"] == sc.VIEW_SCHEMA_VERSION
        assert view["pointer"]["champion"] == "arm_alpha"
        assert view["pointer"]["champion_before"] == "arm_alpha"
        assert all("reason_code" in d for d in view["decisions"])
        assert view["arms"]["arm_alpha"]["n_weeks_paired"] == 1
        assert view["pointer"]["decision_earliest_on"] == "2026-09-25"
        assert view["sources"]["liveness"].endswith("latest.json")

    def test_projection_arms_use_the_records_own_version_keys(self):
        view = _view(
            CUT_SLOT,
            pointer=_v1_record(),
            latest=_v1_record(),
            dates=["2026-08-21"],
            records=[_v1_record()],
            now=datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        arm = view["arms"]["arm_alpha"]
        assert "topn_alpha_vs_population_mean" in arm
        assert "mean_paired_log_return" not in arm

    def test_projection_is_json_serialisable(self):
        import json

        record = _v2_record()
        view = _view(
            CUT_SLOT, pointer=record, latest=record, dates=["2026-08-28"], records=[record]
        )
        json.dumps(view)


class TestNoSilentDegrade:
    def test_loader_has_no_bare_except_pass(self):
        src = LOADER.read_text()
        assert "except: pass" not in src
        assert "except Exception:\n        pass" not in src

    def test_every_slot_declares_its_producer_and_what_it_decides(self):
        for slot in sc.SLOTS:
            assert slot.producer and slot.what_it_decides
            assert slot.pointer_key.startswith("config/")
            assert slot.audit_prefix.startswith("config/apply_audit/")
