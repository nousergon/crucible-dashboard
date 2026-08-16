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


class TestRollingMarks:
    """The observation window has to outlive the deploy that resets the counter.

    alpha-engine-config-I7294. Measured 2026-08-14, minutes after a deploy: 13
    of 15 units were under the 7-day minimum and the only two above it were the
    two nothing had deployed in seventeen days. A window that resets on the
    cadence the box ships code is not an observation window.
    """

    def _marks_file(self, tmp_path):
        return tmp_path / "peak-marks.json"

    def test_a_missing_store_reads_empty_not_zero(self, tmp_path):
        assert cmb.read_peak_marks(tmp_path / "absent.json") == {}

    def test_a_truncated_store_is_discarded_rather_than_half_believed(self, tmp_path):
        p = self._marks_file(tmp_path)
        p.write_text('{"a.service": {"peak_mb": 10,')
        assert cmb.read_peak_marks(p) == {}

    def test_the_mark_is_the_evidence_when_it_beats_the_live_reading(self):
        """A unit restarted an hour ago still carries its pre-restart peak."""
        mark = {"peak_mb": 300, "window_start": "2026-08-01T00:00:00+00:00", "cap_mb": 500}
        now = __import__("datetime").datetime.fromisoformat("2026-08-14T00:00:00+00:00")
        rec = cmb.propose_for_unit(
            _svc(high="350M", hard="500M"),
            peak=20 * MB, high=350 * MB, hard=500 * MB, uptime_days=0.04,
            mark=mark, now=now)
        assert rec["status"] == "propose"
        assert rec["peak_mb"] == 300
        assert rec["uptime_days"] == 13.0
        assert "rolling mark" in rec["observation"]

    def test_a_mark_from_a_different_cap_is_ignored(self):
        """A new cap is a new experiment; the old ceiling is not demand."""
        mark = {"peak_mb": 300, "window_start": "2026-08-01T00:00:00+00:00", "cap_mb": 250}
        rec = cmb.propose_for_unit(
            _svc(high="350M", hard="500M"),
            peak=20 * MB, high=350 * MB, hard=500 * MB, uptime_days=0.04, mark=mark)
        assert rec["status"] == "hold-young"
        assert rec["observation"] == "live memory.peak"

    def test_a_live_peak_above_the_mark_wins(self):
        mark = {"peak_mb": 100, "window_start": "2026-08-01T00:00:00+00:00", "cap_mb": 500}
        now = __import__("datetime").datetime.fromisoformat("2026-08-14T00:00:00+00:00")
        rec = cmb.propose_for_unit(
            _svc(high="350M", hard="500M"),
            peak=200 * MB, high=350 * MB, hard=500 * MB, uptime_days=1, mark=mark, now=now)
        assert rec["peak_mb"] == 200

    def test_a_cache_pinned_unit_is_sized_from_demand_not_from_its_peak(self):
        """dashboard.service, measured 2026-08-16 (config-I7445).

        peak 421 MiB == the 420M soft cap, so the old rule held it forever. But
        183 MiB of that was working set and 224 MiB was reclaimable page cache,
        and cache grows into whatever clearance a cap gives it — so
        `memory.peak` here is a function of the CAP. Sizing from it proposes
        ~1010M on a box with 49 MB of overcommit headroom left; sizing from
        demand proposes 430M.
        """
        mark = {"peak_mb": 421, "ws_peak_mb": 183, "cap_mb": 450,
                "window_start": "2026-08-01T00:00:00+00:00"}
        now = __import__("datetime").datetime.fromisoformat("2026-08-16T00:00:00+00:00")
        rec = cmb.propose_for_unit(
            _svc(high="420M", hard="450M"),
            peak=421 * MB, high=420 * MB, hard=450 * MB, uptime_days=0.5,
            mark=mark, now=now, working_set=183 * MB)
        assert rec["status"] == "propose"
        assert rec["proposed_max_mb"] == 430   # 2.4x the working set, rounded down
        assert rec["direction"] == "lower"
        assert rec["working_set_mb"] == 183
        assert "function of the cap" in rec["detail"]

    def test_a_cache_pinned_unit_with_no_window_is_held_not_proposed(self):
        """The 7-day minimum is not waived by knowing the working set: a
        working set measured over hours has not seen the weekly path either."""
        rec = cmb.propose_for_unit(
            _svc(high="420M", hard="450M"),
            peak=421 * MB, high=420 * MB, hard=450 * MB, uptime_days=0.5,
            working_set=183 * MB)
        assert rec["status"] == "hold-young"
        assert "page cache" in rec["detail"]

    def test_a_working_set_pin_is_still_held(self):
        """The case the hold exists for: the charge IS demand, so the reading
        is a real floor and the remedy is a human raise clear of the pin."""
        mark = {"peak_mb": 421, "ws_peak_mb": 415, "cap_mb": 450,
                "window_start": "2026-08-01T00:00:00+00:00"}
        now = __import__("datetime").datetime.fromisoformat("2026-08-16T00:00:00+00:00")
        rec = cmb.propose_for_unit(
            _svc(high="420M", hard="450M"),
            peak=421 * MB, high=420 * MB, hard=450 * MB, uptime_days=30,
            mark=mark, now=now, working_set=415 * MB)
        assert rec["status"] == "hold-censored"

    def test_an_unknown_working_set_is_held_never_sized(self):
        """No `memory.stat`, no claim. Unknown must not read as "mostly cache"
        — that would size a cap from a number nobody measured."""
        rec = cmb.propose_for_unit(
            _svc(high="420M", hard="450M"),
            peak=421 * MB, high=420 * MB, hard=450 * MB, uptime_days=30,
            working_set=None)
        assert rec["status"] == "hold-censored"

    def test_the_working_set_mark_beats_a_momentary_live_dip(self):
        """A cap must never be sized from an instantaneous low. The rolling
        mark is a high-water mark; the live reading only raises it."""
        mark = {"peak_mb": 421, "ws_peak_mb": 300, "cap_mb": 450,
                "window_start": "2026-08-01T00:00:00+00:00"}
        now = __import__("datetime").datetime.fromisoformat("2026-08-16T00:00:00+00:00")
        rec = cmb.propose_for_unit(
            _svc(high="420M", hard="450M"),
            peak=421 * MB, high=420 * MB, hard=450 * MB, uptime_days=30,
            mark=mark, now=now, working_set=60 * MB)
        assert rec["working_set_mb"] == 300
        assert rec["proposed_max_mb"] == 720

    def test_a_censored_unit_stays_censored_even_with_a_mark(self):
        """Censoring is a LIVE fact; a historical mark must not paper over it."""
        mark = {"peak_mb": 300, "window_start": "2026-08-01T00:00:00+00:00", "cap_mb": 250}
        rec = cmb.propose_for_unit(
            _svc(high="175M", hard="250M"),
            peak=175 * MB, high=175 * MB, hard=250 * MB, uptime_days=30, mark=mark)
        assert rec["status"] == "hold-censored"

    def test_the_window_survives_a_restart(self, tmp_path, monkeypatch):
        p = self._marks_file(tmp_path)
        dt = __import__("datetime")
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 300 * MB)
        t0 = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        cmb.update_peak_marks([("a.service", 500)], now=t0, path=p)
        # The unit restarts: its cgroup counter drops to a fresh, low peak.
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 20 * MB)
        marks = cmb.update_peak_marks(
            [("a.service", 500)], now=t0 + dt.timedelta(days=9), path=p)
        assert marks["a.service"]["peak_mb"] == 300, "the restart erased the evidence"
        assert marks["a.service"]["window_start"] == "2026-08-01T00:00:00+00:00"
        assert round(cmb.mark_window_days(marks["a.service"],
                                          t0 + dt.timedelta(days=9))) == 9

    def test_a_cap_change_resets_the_window(self, tmp_path, monkeypatch):
        p = self._marks_file(tmp_path)
        dt = __import__("datetime")
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 300 * MB)
        t0 = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        cmb.update_peak_marks([("a.service", 500)], now=t0, path=p)
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 40 * MB)
        marks = cmb.update_peak_marks(
            [("a.service", 900)], now=t0 + dt.timedelta(days=3), path=p)
        assert marks["a.service"]["peak_mb"] == 40
        assert marks["a.service"]["window_start"] == "2026-08-04T00:00:00+00:00"

    def test_an_observe_only_unit_is_tracked_without_a_cap(self, tmp_path, monkeypatch):
        p = self._marks_file(tmp_path)
        dt = __import__("datetime")
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 1689 * MB)
        marks = cmb.update_peak_marks(
            [("amazon-ssm-agent.service", None)],
            now=dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc), path=p)
        assert marks["amazon-ssm-agent.service"]["peak_mb"] == 1689
        assert marks["amazon-ssm-agent.service"]["cap_mb"] is None

    def test_the_mark_tracks_the_working_set_high_water_too(self, tmp_path, monkeypatch):
        """A cache-pinned unit can only be sized from demand if demand has a
        window behind it — and `memory.stat` has no cgroup-provided high-water
        counterpart to `memory.peak`, so this mark IS the record."""
        p = self._marks_file(tmp_path)
        dt = __import__("datetime")
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 421 * MB)
        monkeypatch.setattr(cmb, "non_reclaimable_bytes", lambda unit: 183 * MB)
        t0 = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        cmb.update_peak_marks([("a.service", 450)], now=t0, path=p)
        # A later tick reads a LOWER working set: the high-water mark holds.
        monkeypatch.setattr(cmb, "non_reclaimable_bytes", lambda unit: 90 * MB)
        marks = cmb.update_peak_marks(
            [("a.service", 450)], now=t0 + dt.timedelta(days=2), path=p)
        assert marks["a.service"]["ws_peak_mb"] == 183
        # And a cap change resets it with everything else — a new cap is a new
        # experiment, and cache behaviour changes with the ceiling.
        marks = cmb.update_peak_marks(
            [("a.service", 300)], now=t0 + dt.timedelta(days=3), path=p)
        assert marks["a.service"]["ws_peak_mb"] == 90

    def test_an_unreadable_memory_stat_leaves_the_mark_without_a_working_set(
        self, tmp_path, monkeypatch
    ):
        """Absent is absent. A zero here would propose a 64M cap on a service
        whose demand was never measured."""
        p = self._marks_file(tmp_path)
        dt = __import__("datetime")
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 421 * MB)
        monkeypatch.setattr(cmb, "non_reclaimable_bytes", lambda unit: None)
        marks = cmb.update_peak_marks(
            [("a.service", 450)],
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), path=p)
        assert "ws_peak_mb" not in marks["a.service"]

    def test_the_store_is_written_atomically(self, tmp_path, monkeypatch):
        p = self._marks_file(tmp_path)
        dt = __import__("datetime")
        monkeypatch.setattr(cmb, "cgroup_value", lambda unit, f: 10 * MB)
        cmb.update_peak_marks([("a.service", 100)],
                              now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), path=p)
        assert p.exists() and not p.with_suffix(".json.tmp").exists()
        assert json.loads(p.read_text())["a.service"]["peak_mb"] == 10


class TestObserveOnlyDisposition:
    """I5276: 'not mentioned anywhere' is not an acceptable end state."""

    def test_both_aws_agents_carry_a_written_disposition(self):
        spec = yaml.safe_load(BUDGET.read_text())
        units = {e["unit"]: e for e in spec.get("observe_only", [])}
        assert "amazon-ssm-agent.service" in units
        assert "amazon-cloudwatch-agent.service" in units
        for entry in units.values():
            assert len(entry.get("reason", "")) > 80, (
                "an uncapped unit needs a stated reason, not a name on a list")

    def test_observe_only_units_are_not_in_the_capped_sum(self):
        spec = yaml.safe_load(BUDGET.read_text())
        capped = {s["unit"] for s in spec["services"]}
        for entry in spec["observe_only"]:
            assert entry["unit"] not in capped


class TestTimerJobCapsMatchTheirDropIn:
    """budget.yaml DECLARES timer-job caps; the drop-in APPLIES them.

    `install-resource-limits.sh` deliberately does not render timer jobs — their
    caps live with the unit that owns them — so the declaration and the applied
    value are two files that can disagree silently. On-box drift detection
    catches that only after a deploy; this catches it in CI.
    """

    DROPIN = (REPO_ROOT / "infrastructure" / "systemd" /
              "morning-signal.service.d" / "10-memory.conf")

    def _declared(self):
        spec = yaml.safe_load(BUDGET.read_text())
        return next(j for j in spec["timer_jobs"]
                    if j["unit"] == "morning-signal.service")

    def test_the_dropin_applies_what_budget_yaml_declares(self):
        job = self._declared()
        body = self.DROPIN.read_text()
        assert f"MemoryHigh={job['memory_high']}" in body
        assert f"MemoryMax={job['memory_max']}" in body

    def test_the_cap_is_backed_by_a_measurement(self):
        job = self._declared()
        assert job.get("measured_peak_mb"), (
            "a timer-job cap with measured_peak_mb null is an estimate — I5546")

    def test_the_cap_sits_in_the_band_over_its_measured_peak(self):
        job = self._declared()
        hard = cmb.parse_bytes(job["memory_max"]) // MB
        peak = job["measured_peak_mb"]
        assert peak * 2.0 <= hard <= peak * cmb.OVER_PROVISION_RATIO, (
            f"{hard}M against a {peak} MiB peak is {hard / peak:.1f}x — outside "
            f"the 2.0-{cmb.OVER_PROVISION_RATIO}x band this box sizes to")

    def test_the_peak_recorder_is_still_installed(self):
        """The cap above is only re-derivable while the recorder keeps logging."""
        rec = (REPO_ROOT / "infrastructure" / "systemd" /
               "morning-signal.service.d" / "30-record-peak.conf")
        assert "ExecStopPost=" in rec.read_text()
        deploy = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "30-record-peak.conf:/etc/systemd/system" in deploy, (
            "a recorder the deploy gate cannot see stops reaching the box")
