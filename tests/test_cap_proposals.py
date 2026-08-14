"""The cap-proposal loop (alpha-engine-config-I7291).

Two things are under test and they fail in different ways:

  1. The DERIVATION — arithmetic over a measured peak. Its failure mode is a
     number that is wrong, or one that instantly re-trips the checks it was
     derived to satisfy, which turns a proposal loop into a PR treadmill.
  2. The EDITOR — a line-precise rewrite of budget.yaml. Its failure mode is
     collateral: a lost `note:`, a reflowed comment, a mangled block. The file
     is mostly institutional memory, so an edit that changes anything it was
     not asked to change is a defect even when the numbers land correctly.

Both are exercised against the REAL budget.yaml rather than a fixture, because
the shape this editor has to survive is the shape that file actually has.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BUDGET = REPO_ROOT / "infrastructure" / "systemd" / "resource-limits" / "budget.yaml"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "infrastructure" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cmb = _load("check_memory_budget")
acp = _load("apply_cap_proposals")

MB = 1024 ** 2


class TestDerivation:
    def test_max_is_the_landed_band_over_the_peak(self):
        high, hard = cmb.derive_caps(100 * MB)
        assert hard == 240   # 2.4x, the #662 band
        assert high == 170   # 70% of max, rounded up to the next 10 MB

    def test_a_fresh_proposal_never_reads_as_over_provisioned(self):
        """The loop must not immediately re-report what it just proposed."""
        for peak_mb in (40, 90, 128, 175, 287, 450):
            _, hard = cmb.derive_caps(peak_mb * MB)
            assert hard > cmb.PROPOSAL_MIN_MAX_MB, "peak too small to test the band"
            assert hard < cmb.OVER_PROVISION_RATIO * peak_mb, (
                f"a {hard}M cap derived from a {peak_mb} MiB peak is "
                f"{hard / peak_mb:.2f}x it, at or past the "
                f"{cmb.OVER_PROVISION_RATIO}x over-provision line")

    def test_a_fresh_proposal_is_not_already_approaching_its_cap(self):
        for peak_mb in (40, 90, 128, 175, 287, 450):
            high, _ = cmb.derive_caps(peak_mb * MB)
            assert peak_mb < cmb.APPROACHING_FRACTION * high, (
                f"a {high}M soft cap leaves a {peak_mb} MiB peak already at "
                f"{100 * peak_mb / high:.0f}% of it")

    def test_the_reclaim_window_always_survives_rounding(self):
        for peak_mb in range(1, 600, 7):
            high, hard = cmb.derive_caps(peak_mb * MB)
            assert high < hard, f"peak {peak_mb} MiB collapsed high onto max"

    def test_tiny_peaks_do_not_produce_absurd_caps(self):
        _, hard = cmb.derive_caps(3 * MB)
        assert hard == cmb.PROPOSAL_MIN_MAX_MB

    def test_proposal_and_over_provision_share_one_uptime_minimum(self):
        assert cmb.PROPOSAL_MIN_UPTIME_DAYS == cmb.OVER_PROVISION_MIN_UPTIME_DAYS


def _svc(high="175M", hard="250M", **extra):
    return {"unit": "x.service", "memory_high": high, "memory_max": hard, **extra}


class TestPerUnitHolds:
    def test_a_censored_unit_is_never_sized_from_its_floor(self):
        rec = cmb.propose_for_unit(
            _svc(), peak=175 * MB, high=175 * MB, hard=250 * MB, uptime_days=30)
        assert rec["status"] == "hold-censored"
        assert "proposed_max_mb" not in rec

    def test_a_freshly_restarted_unit_is_held(self):
        rec = cmb.propose_for_unit(
            _svc(), peak=60 * MB, high=175 * MB, hard=250 * MB, uptime_days=2)
        assert rec["status"] == "hold-young"

    def test_unknown_uptime_is_held_not_assumed(self):
        rec = cmb.propose_for_unit(
            _svc(), peak=60 * MB, high=175 * MB, hard=250 * MB, uptime_days=None)
        assert rec["status"] == "hold-young"

    def test_a_unit_with_no_cgroup_reading_is_recorded_not_dropped(self):
        rec = cmb.propose_for_unit(
            _svc(), peak=None, high=None, hard=None, uptime_days=30)
        assert rec["status"] == "hold-unmeasurable"
        assert rec["unit"] == "x.service"

    def test_an_uncapped_unit_is_not_proposed_against(self):
        rec = cmb.propose_for_unit(
            _svc(), peak=60 * MB, high=175 * MB, hard=sys.maxsize, uptime_days=30)
        assert rec["status"] == "hold-unmeasurable"

    def test_the_opt_out_key_is_honoured(self):
        rec = cmb.propose_for_unit(
            _svc(cap_proposals="manual"),
            peak=60 * MB, high=175 * MB, hard=250 * MB, uptime_days=30)
        assert rec["status"] == "hold-manual"

    def test_a_cap_already_close_to_its_derivation_is_left_alone(self):
        # 100 MiB peak derives 170M/240M; 250M declared is inside tolerance.
        rec = cmb.propose_for_unit(
            _svc(high="160M", hard="250M"),
            peak=100 * MB, high=160 * MB, hard=250 * MB, uptime_days=30)
        assert rec["status"] == "ok"

    def test_a_measured_lowering_is_proposed(self):
        rec = cmb.propose_for_unit(
            _svc(high="350M", hard="500M"),
            peak=100 * MB, high=350 * MB, hard=500 * MB, uptime_days=30)
        assert rec["status"] == "propose"
        assert rec["direction"] == "lower"
        assert (rec["proposed_high_mb"], rec["proposed_max_mb"]) == (170, 240)

    def test_a_measured_raise_is_proposed(self):
        rec = cmb.propose_for_unit(
            _svc(high="100M", hard="150M"),
            peak=90 * MB, high=100 * MB, hard=150 * MB, uptime_days=30)
        assert rec["status"] == "propose"
        assert rec["direction"] == "raise"


class TestOvercommitGuard:
    def _rec(self, unit, declared, proposed, direction):
        return {"unit": unit, "status": "propose", "declared_max_mb": declared,
                "declared_high_mb": int(declared * 0.7), "proposed_max_mb": proposed,
                "proposed_high_mb": int(proposed * 0.7), "direction": direction,
                "peak_mb": 10, "uptime_days": 30, "detail": ""}

    def test_a_raise_that_breaches_the_bound_is_blocked_not_shaved(self):
        recs = cmb.enforce_overcommit(
            [self._rec("a.service", 100, 400, "raise"),
             self._rec("b.service", 100, 130, "raise")], bound_mb=300)
        blocked = [r for r in recs if r["status"] == "blocked-overcommit"]
        assert [r["unit"] for r in blocked] == ["a.service"]
        # The whole point: the blocked unit keeps its declared cap and is NOT
        # silently resized to whatever fits.
        assert "proposed_max_mb" not in blocked[0]
        assert "shave" in blocked[0]["detail"]
        assert recs[1]["status"] == "propose"

    def test_lowerings_are_never_blocked(self):
        recs = cmb.enforce_overcommit(
            [self._rec("a.service", 500, 200, "lower")], bound_mb=100)
        assert recs[0]["status"] == "propose"

    def test_a_set_that_fits_is_untouched(self):
        recs = cmb.enforce_overcommit(
            [self._rec("a.service", 100, 150, "raise")], bound_mb=1000)
        assert recs[0]["status"] == "propose"


class TestEditor:
    """The editor is judged on what it does NOT change."""

    def _payload(self, unit, high, hard, declared_high, declared_max):
        return {"records": [{
            "unit": unit, "status": "propose",
            "declared_high_mb": declared_high, "declared_max_mb": declared_max,
            "proposed_high_mb": high, "proposed_max_mb": hard,
            "peak_mb": 120, "uptime_days": 9.4, "direction": "lower", "detail": "",
        }]}

    def test_it_edits_the_two_lines_and_adds_a_dated_note(self):
        text = BUDGET.read_text()
        recs = self._payload("mnemon.service", 200, 280, 175, 250)["records"]
        new, changed = acp.apply_records(text, recs, date="2026-08-14")
        assert changed == ["mnemon.service"]
        spec = yaml.safe_load(new)
        entry = next(s for s in spec["services"] if s["unit"] == "mnemon.service")
        assert (entry["memory_high"], entry["memory_max"]) == ("200M", "280M")
        assert "2026-08-14 (automated, alpha-engine-config-I7291)" in entry["note"]

    def test_no_other_unit_moves(self):
        text = BUDGET.read_text()
        before = yaml.safe_load(text)
        recs = self._payload("mnemon.service", 200, 280, 175, 250)["records"]
        new, _ = acp.apply_records(text, recs, date="2026-08-14")
        after = yaml.safe_load(new)
        for b, a in zip(before["services"], after["services"]):
            if b["unit"] == "mnemon.service":
                continue
            assert b == a, f"{b['unit']} changed and should not have"

    def test_every_line_it_was_not_asked_to_touch_is_byte_identical(self):
        text = BUDGET.read_text()
        recs = self._payload("mnemon.service", 200, 280, 175, 250)["records"]
        new, _ = acp.apply_records(text, recs, date="2026-08-14")
        before, after = text.splitlines(), new.splitlines()
        # Only insertions (the note paragraph) and the two scalar edits.
        removed = [ln for ln in before if ln not in after]
        assert all("memory_high" in ln or "memory_max" in ln for ln in removed), removed

    def test_the_existing_note_survives_and_is_prepended_to(self):
        text = BUDGET.read_text()
        entry_before = next(s for s in yaml.safe_load(text)["services"]
                            if s["unit"] == "llm-egress-proxy.service")
        recs = self._payload("llm-egress-proxy.service", 200, 280, 210, 300)["records"]
        new, _ = acp.apply_records(text, recs, date="2026-08-14")
        entry = next(s for s in yaml.safe_load(new)["services"]
                     if s["unit"] == "llm-egress-proxy.service")
        assert "Calibrated 2026-07-27" in entry["note"], "the unit's standing argument was erased"
        assert entry["note"].index("2026-08-14 (automated") < entry["note"].index("Calibrated 2026-07-27")

    def test_applying_the_same_proposal_twice_is_a_no_op(self):
        text = BUDGET.read_text()
        recs = self._payload("mnemon.service", 200, 280, 175, 250)["records"]
        once, _ = acp.apply_records(text, recs, date="2026-08-14")
        twice, changed = acp.apply_records(once, recs, date="2026-08-14")
        assert changed == []
        assert twice == once

    def test_holds_are_never_written(self):
        text = BUDGET.read_text()
        new, changed = acp.apply_records(
            text, [{"unit": "mnemon.service", "status": "hold-censored"}], date="2026-08-14")
        assert changed == [] and new == text

    def test_a_unit_the_repo_does_not_declare_is_an_error_not_a_skip(self):
        recs = self._payload("ghost.service", 200, 280, 175, 250)["records"]
        with pytest.raises(KeyError):
            acp.apply_records(BUDGET.read_text(), recs, date="2026-08-14")

    def test_the_result_still_passes_the_declared_budget_check(self):
        """An applied proposal must not produce a budget.yaml that fails CI."""
        text = BUDGET.read_text()
        recs = self._payload("mnemon.service", 140, 200, 175, 250)["records"]
        new, _ = acp.apply_records(text, recs, date="2026-08-14")
        spec = yaml.safe_load(new)
        ceiling = int(spec["ram_mb"] * (1 - spec["reserve_fraction"]))
        total = sum(cmb.parse_bytes(s["memory_max"]) for s in spec["services"]) // MB
        assert total <= ceiling * spec["max_overcommit_ratio"]


class TestWorkflowContract:
    """The workflow is the only caller; its assumptions are asserted here."""

    WF = REPO_ROOT / ".github" / "workflows" / "propose-memory-caps.yml"

    def test_the_workflow_exists_and_is_scheduled(self):
        body = self.WF.read_text()
        assert "schedule:" in body and "workflow_dispatch" in body

    def test_it_never_merges_what_it_opens(self):
        body = self.WF.read_text()
        assert "gh pr merge" not in body, (
            "the merge is Brian's — auto-merge-policy.md. This loop automates "
            "the derivation and the proposal, never the decision.")

    def test_it_reads_the_box_rather_than_guessing(self):
        body = self.WF.read_text()
        assert "--propose-caps" in body and "ssm send-command" in body

    def test_it_passes_an_explicit_date_so_a_rerun_is_reproducible(self):
        assert "--date" in self.WF.read_text()

    def test_the_json_the_workflow_consumes_is_the_json_the_box_emits(self):
        """Producer/consumer contract, asserted rather than assumed."""
        rec = cmb.propose_for_unit(
            _svc(high="350M", hard="500M"),
            peak=100 * MB, high=350 * MB, hard=500 * MB, uptime_days=30)
        payload = {"schema_version": 1, "records": [rec], "sum_max_before_mb": 1,
                   "sum_max_after_mb": 1, "overcommit_bound_mb": 1}
        # json round-trip is what SSM actually delivers to the applier.
        rec = json.loads(json.dumps(payload))["records"][0]
        rec["unit"] = "mnemon.service"
        new, changed = acp.apply_records(BUDGET.read_text(), [rec], date="2026-08-14")
        assert changed == ["mnemon.service"]
