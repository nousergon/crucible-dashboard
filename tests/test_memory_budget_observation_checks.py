"""The steady-state bound must be MEASURED from the box, and honest about it.

WHY THESE EXIST
---------------
`max_steady_state_fraction` is the bound budget.yaml itself calls "the one that
governs normal operation". Until 2026-07-29 its left-hand side was a
hand-maintained `observed_mb:` per service -- a fixed number describing a
continuously-moving quantity, which went wrong the only way it could. It was
re-measured by hand three times (litellm-proxy, metron-api/config-I5216,
dashboard/config-I5237), and on the first day anything compared it to the box,
three of fourteen units disagreed with their own entry, one by 2.8x.

`observed_mb` is gone; the sum is read from each unit's `memory.current`. What
survives is the check that says when that reading cannot be trusted:
`memory.peak >= memory.high` means the cgroup has been pinned at its soft cap,
so the reading is a floor. That matters MORE now, not less, because the floor
now feeds the bound directly -- which is why steady_state_mb returns its
caveats rather than a bare number.
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
        msg = cmb.censored_observation("dashboard.service")
        assert msg is not None
        assert "CENSORED" in msg
        # The actionable instruction is the point of the message, not the label.
        assert "FLOOR" in msg
        assert "do NOT re-cap to just above the pinned number" in msg

    def test_peak_above_high_is_censored(self, cgroup_root):
        """peak can exceed high -- high throttles, it does not hard-stop."""
        _cgroup(cgroup_root, "svc.service",
                current=250 * MB, peak=261 * MB, high=260 * MB)
        assert cmb.censored_observation("svc.service") is not None

    def test_peak_clear_of_high_is_not_censored(self, cgroup_root):
        """metron-api after the single-worker cut: 203 peak against a 280 cap."""
        _cgroup(cgroup_root, "metron-api.service",
                current=202 * MB, peak=203 * MB, high=280 * MB)
        assert cmb.censored_observation("metron-api.service") is None

    def test_infinite_high_is_not_censored(self, cgroup_root):
        """No cap means the reading cannot be bounded by one.

        Uncapped is a real defect, but it is a DIFFERENT one and is already
        reported separately. Flagging it here too would double-report it and
        blur what "censored" means.
        """
        _cgroup(cgroup_root, "svc.service",
                current=900 * MB, peak=900 * MB, high="max")
        assert cmb.censored_observation("svc.service") is None

    def test_missing_cgroup_returns_none_rather_than_passing(self, cgroup_root):
        """A unit that is not running has no cgroup. That is not a finding."""
        assert cmb.censored_observation("absent.service") is None

    def test_unreadable_value_does_not_crash_the_whole_check(self, cgroup_root):
        d = _cgroup(cgroup_root, "svc.service", peak=100 * MB, high=200 * MB)
        (d / "memory.peak").write_text("garbage\n")
        assert cmb.censored_observation("svc.service") is None


class TestSteadyStateIsMeasured:
    """The bound's left-hand side comes from the box, with its caveats attached.

    A declared `observed_mb` could be wrong in the safe direction indefinitely
    and nothing would know. A measured sum can only be wrong in two ways --
    a unit it could not read, and a unit whose reading is pinned -- and both are
    knowable, so both are returned rather than folded into the number.
    """

    def test_sums_live_readings(self, cgroup_root):
        _cgroup(cgroup_root, "a.service", current=100 * MB, peak=50 * MB, high=400 * MB)
        _cgroup(cgroup_root, "b.service", current=59 * MB, peak=50 * MB, high=400 * MB)
        total, unmeasurable, censored = cmb.steady_state_mb(["a.service", "b.service"])
        assert total == 159
        assert unmeasurable == [] and censored == []

    def test_declared_values_cannot_influence_the_sum(self, cgroup_root):
        """The regression that motivated the whole change.

        nous-ergon-live declared 56 MB and held 159 MB. Under the old code the
        bound was computed from 56 and the 159 produced a page about the file.
        There is now no path by which a number in budget.yaml can reach this
        sum at all -- which is what makes the failure class extinct rather than
        merely fixed.
        """
        _cgroup(cgroup_root, "nous-ergon-live.service",
                current=159 * MB, peak=60 * MB, high=175 * MB)
        total, _, _ = cmb.steady_state_mb(["nous-ergon-live.service"])
        assert total == 159

    def test_unreadable_unit_is_named_not_counted_as_zero(self, cgroup_root):
        """Counting a missing unit as 0 makes the bound read safer than it is."""
        _cgroup(cgroup_root, "a.service", current=100 * MB, peak=10 * MB, high=400 * MB)
        total, unmeasurable, _ = cmb.steady_state_mb(["a.service", "gone.service"])
        assert total == 100
        assert unmeasurable == ["gone.service"]

    def test_censored_unit_is_counted_but_flagged(self, cgroup_root):
        """Its reading is a floor: usable as a lower bound, not as proof."""
        _cgroup(cgroup_root, "pinned.service",
                current=340 * MB, peak=340 * MB, high=340 * MB)
        total, unmeasurable, censored = cmb.steady_state_mb(["pinned.service"])
        assert total == 340
        assert unmeasurable == []
        assert censored == ["pinned.service"]


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

    def test_no_service_declares_a_steady_state_number(self):
        """`observed_mb` must not come back.

        It is the natural thing to re-add -- it reads like documentation and it
        makes --declared able to check the steady-state bound again. Both are
        traps: it is a cache of a live value with no invalidation, and the
        version of the bound it enables is only as good as numbers nobody can
        verify. The failure class is extinct only while the field is absent, so
        the absence is asserted rather than trusted to review.
        """
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        offenders = [s["unit"] for s in spec["services"] if "observed_mb" in s]
        assert not offenders, (
            f"{offenders} declare observed_mb. Steady state is measured from "
            "memory.current at check time -- see the module docstring in "
            "check_memory_budget.py."
        )

    def test_steady_state_bound_is_still_declared(self):
        """Removing the measurements must not remove the LIMIT.

        The fraction is the half a human legitimately owns; deleting it along
        with the numbers would quietly retire the bound entirely.
        """
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        assert 0 < float(spec["max_steady_state_fraction"]) <= 1
