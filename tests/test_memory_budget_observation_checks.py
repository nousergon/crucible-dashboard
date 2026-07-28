"""budget.yaml's observed_mb values must be checkable against the live box.

WHY THESE EXIST
---------------
`observed_mb` feeds `max_steady_state_fraction` -- the bound budget.yaml itself
calls "the one that governs normal operation". Until now nothing verified those
numbers against the box, and they were wrong three separate times in the same
way: measured while the service was pinned at its own soft cap, so the reading
was bounded by the cap rather than by the service.

`memory.peak >= memory.high` is the tell. It was found BY HAND three times
(litellm-proxy, metron-api/config-I5216, dashboard/config-I5237) and written
into a prose `note:` each time instead of a check. Each time the cap was then
raised to just above the censored floor, and each time the service re-pinned --
config-I5237 moved dashboard.service 210M -> 260M and it was throttling again
within a day, because 202 MiB was never its working set.

A defect found three times by hand and recorded in prose is a check waiting to
be written. This is that check.
"""

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_memory_budget", REPO_ROOT / "infrastructure" / "check_memory_budget.py"
)
cmb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cmb)

MB = 1024**2


def _cgroup(tmp_path, unit, *, current=None, peak=None, high=None):
    """Build a fake cgroup v2 tree for one unit."""
    d = tmp_path / unit
    d.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("memory.current", current),
        ("memory.peak", peak),
        ("memory.high", high),
    ):
        if value is not None:
            (d / name).write_text("max\n" if value == "max" else f"{value}\n")
    return d


@pytest.fixture
def cgroup_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cmb, "_CGROUP_ROOT", tmp_path)
    return tmp_path


class TestCensoredObservation:
    def test_peak_at_high_is_reported_as_censored(self, cgroup_root):
        """The exact dashboard.service shape on 2026-07-28, pre-fix."""
        _cgroup(cgroup_root, "dashboard.service",
                current=250 * MB, peak=260 * MB, high=260 * MB)
        msg = cmb.censored_observation("dashboard.service", 202)
        assert msg is not None
        assert "CENSORED" in msg
        # The actionable instruction is the point of the message, not the label.
        assert "FLOOR" in msg
        assert "do NOT re-cap to just above this number" in msg

    def test_peak_above_high_is_censored(self, cgroup_root):
        """peak can exceed high -- high throttles, it does not hard-stop."""
        _cgroup(cgroup_root, "svc.service",
                current=250 * MB, peak=261 * MB, high=260 * MB)
        assert cmb.censored_observation("svc.service", 202) is not None

    def test_peak_clear_of_high_is_not_censored(self, cgroup_root):
        """metron-api after the single-worker cut: 203 peak against a 280 cap."""
        _cgroup(cgroup_root, "metron-api.service",
                current=202 * MB, peak=203 * MB, high=280 * MB)
        assert cmb.censored_observation("metron-api.service", 203) is None

    def test_infinite_high_is_not_censored(self, cgroup_root):
        """No cap means the reading cannot be bounded by one.

        Uncapped is a real defect, but it is a DIFFERENT one and is already
        reported separately. Flagging it here too would double-report it and
        blur what "censored" means.
        """
        _cgroup(cgroup_root, "svc.service",
                current=900 * MB, peak=900 * MB, high="max")
        assert cmb.censored_observation("svc.service", 100) is None

    def test_missing_cgroup_returns_none_rather_than_passing(self, cgroup_root):
        """A unit that is not running has no cgroup. That is not a finding."""
        assert cmb.censored_observation("absent.service", 100) is None

    def test_unreadable_value_does_not_crash_the_whole_check(self, cgroup_root):
        d = _cgroup(cgroup_root, "svc.service", peak=100 * MB, high=200 * MB)
        (d / "memory.peak").write_text("garbage\n")
        assert cmb.censored_observation("svc.service", 50) is None


class TestStaleObservation:
    def test_current_far_above_declared_is_reported(self, cgroup_root):
        """The config-I5237 shape: declared 82, actually 202."""
        _cgroup(cgroup_root, "dashboard.service",
                current=202 * MB, peak=205 * MB, high=400 * MB)
        msg = cmb.stale_observation("dashboard.service", 82)
        assert msg is not None
        assert "STALE" in msg
        assert "82" in msg and "202" in msg

    def test_within_tolerance_is_quiet(self, cgroup_root):
        """observed_mb is a steady-state figure; normal drift is not a defect."""
        _cgroup(cgroup_root, "svc.service", current=110 * MB, high=400 * MB)
        assert cmb.stale_observation("svc.service", 100) is None

    def test_using_less_than_declared_is_not_stale(self, cgroup_root):
        """Only understatement corrupts the steady-state SUM upward."""
        _cgroup(cgroup_root, "svc.service", current=40 * MB, high=400 * MB)
        assert cmb.stale_observation("svc.service", 100) is None

    def test_zero_declared_does_not_divide_by_zero(self, cgroup_root):
        _cgroup(cgroup_root, "svc.service", current=40 * MB, high=400 * MB)
        assert cmb.stale_observation("svc.service", 0) is None


class TestOrphanDropins:
    def _dropin(self, root, unit, name, body):
        d = root / f"{unit}.d"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)

    @pytest.fixture
    def dropin_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cmb, "_DROPIN_ROOT", tmp_path)
        return tmp_path

    def test_dropin_for_unknown_unit_is_reported(self, dropin_root):
        """The real orphan found on the box, 2026-07-28."""
        self._dropin(dropin_root, "alpha-engine-dashboard.service",
                     "memory-limit.conf",
                     "[Service]\nMemoryMax=300M\nMemoryHigh=250M\n")
        found = cmb.orphan_dropins({"dashboard.service"})
        assert len(found) == 1
        assert "ORPHAN" in found[0]
        assert "memory-limit.conf" in found[0]
        # No unit file exists in the fixture tree, so it must say so -- that is
        # what distinguishes "stale cap on a live service" from "dead file".
        assert "no unit file on disk" in found[0]

    def test_budget_owned_dropin_is_not_an_orphan(self, dropin_root):
        self._dropin(dropin_root, "dashboard.service", "99-resource-limits.conf",
                     "[Service]\nMemoryMax=450M\nMemoryHigh=340M\n")
        assert cmb.orphan_dropins({"dashboard.service"}) == []

    def test_allowlisted_unit_is_not_an_orphan(self, dropin_root):
        """morning-signal is timer-driven and deliberately out of budget scope."""
        self._dropin(dropin_root, "morning-signal.service", "10-memory.conf",
                     "[Service]\nMemoryHigh=600M\n")
        assert cmb.orphan_dropins({"dashboard.service"}) == []

    def test_dropin_without_memory_settings_is_ignored(self, dropin_root):
        """This check owns memory limits only, not every drop-in on the box."""
        self._dropin(dropin_root, "whatever.service", "10-after-news.conf",
                     "[Unit]\nAfter=daily-news.service\n")
        assert cmb.orphan_dropins({"dashboard.service"}) == []


class TestBudgetFileIsInternallyConsistent:
    """The declared bounds must hold for the file as committed.

    --declared is the CI mode and is what stops an over-budget set from ever
    shipping; running it here means a cap edit cannot merge without satisfying
    the same arithmetic the installer refuses to violate.
    """

    def test_declared_budget_passes(self, capsys):
        assert cmb.main.__module__  # sanity: module loaded
        import sys
        argv = sys.argv
        sys.argv = ["check_memory_budget.py", "--declared", "--quiet"]
        try:
            assert cmb.main() == 0
        finally:
            sys.argv = argv
