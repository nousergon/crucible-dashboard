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


# budget.yaml's `headroom_warn_fraction`. Passed explicitly rather than
# defaulted in the function, so a change to the declared value cannot silently
# diverge from what these tests assert.
WARN = 0.90


class TestCensoredObservation:
    def test_peak_at_high_is_reported_as_censored(self, cgroup_root):
        """The exact dashboard.service shape on 2026-07-28, pre-fix."""
        _cgroup(cgroup_root, "dashboard.service",
                current=250 * MB, peak=260 * MB, high=260 * MB)
        msg = cmb.censored_observation("dashboard.service", WARN)
        assert msg is not None
        assert "CENSORED" in msg
        # The actionable instruction is the point of the message, not the label.
        assert "FLOOR" in msg
        assert "do NOT re-cap to just above the pinned number" in msg

    def test_peak_above_high_is_censored(self, cgroup_root):
        """peak can exceed high -- high throttles, it does not hard-stop."""
        _cgroup(cgroup_root, "svc.service",
                current=250 * MB, peak=261 * MB, high=260 * MB)
        assert cmb.censored_observation("svc.service", WARN) is not None

    def test_peak_clear_of_high_is_not_censored(self, cgroup_root):
        """metron-api after the single-worker cut: 203 peak against a 280 cap."""
        _cgroup(cgroup_root, "metron-api.service",
                current=202 * MB, peak=203 * MB, high=280 * MB)
        assert cmb.censored_observation("metron-api.service", WARN) is None

    def test_infinite_high_is_not_censored(self, cgroup_root):
        """No cap means the reading cannot be bounded by one.

        Uncapped is a real defect, but it is a DIFFERENT one and is already
        reported separately. Flagging it here too would double-report it and
        blur what "censored" means.
        """
        _cgroup(cgroup_root, "svc.service",
                current=900 * MB, peak=900 * MB, high="max")
        assert cmb.censored_observation("svc.service", WARN) is None

    def test_missing_cgroup_returns_none_rather_than_passing(self, cgroup_root):
        """A unit that is not running has no cgroup. That is not a finding."""
        assert cmb.censored_observation("absent.service", WARN) is None

    def test_unreadable_value_does_not_crash_the_whole_check(self, cgroup_root):
        d = _cgroup(cgroup_root, "svc.service", peak=100 * MB, high=200 * MB)
        (d / "memory.peak").write_text("garbage\n")
        assert cmb.censored_observation("svc.service", WARN) is None


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
        total, unmeasurable, censored = cmb.steady_state_mb(["a.service", "b.service"], WARN)
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
        total, _, _ = cmb.steady_state_mb(["nous-ergon-live.service"], WARN)
        assert total == 159

    def test_unreadable_unit_is_named_not_counted_as_zero(self, cgroup_root):
        """Counting a missing unit as 0 makes the bound read safer than it is."""
        _cgroup(cgroup_root, "a.service", current=100 * MB, peak=10 * MB, high=400 * MB)
        total, unmeasurable, _ = cmb.steady_state_mb(["a.service", "gone.service"], WARN)
        assert total == 100
        assert unmeasurable == ["gone.service"]

    def test_censored_unit_is_counted_but_flagged(self, cgroup_root):
        """Its reading is a floor: usable as a lower bound, not as proof."""
        _cgroup(cgroup_root, "pinned.service",
                current=340 * MB, peak=340 * MB, high=340 * MB)
        total, unmeasurable, censored = cmb.steady_state_mb(["pinned.service"], WARN)
        assert total == 340
        assert unmeasurable == []
        assert censored == ["pinned.service"]


class TestTimerJobBudget:
    """Timer-driven jobs are DECLARED, not suppressed.

    `services:` is a concurrency bound — everything in it runs continuously, so
    the caps can sum above RAM at the same instant. A timer oneshot is not in
    that set (metron-intraday runs ~3s every 5 min) but it still allocates ON TOP
    of whatever the services hold, so it needs its own bound: it must fit in the
    headroom they leave.

    Before 2026-07-29 these were invisible. morning-signal.service's 900M cap —
    the largest single claim on this box's headroom — was known to the budget
    only as a name on `DROPIN_ALLOW`, an ignore list. Invisible is not the same
    as fine.
    """

    def _spec(self):
        import yaml
        return yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )

    def test_every_timer_job_declares_both_caps(self):
        for job in self._spec().get("timer_jobs", []):
            assert job.get("memory_max"), f"{job['unit']} has no memory_max"
            assert job.get("memory_high"), (
                f"{job['unit']} has no memory_high — no reclaim window before the "
                "hard cap, the same defect the services list records for mnemon"
            )

    def test_dropin_allowlist_is_empty(self):
        """A suppression and a declaration are not interchangeable.

        An entry on the allowlist asserts "we looked and it is fine" and nothing
        ever re-examines it; a `timer_jobs:` row is drift-checked every 10
        minutes. If a unit needs to come off the check, it should get a row, not
        a name here.
        """
        assert cmb.DROPIN_ALLOW == set(), (
            f"{cmb.DROPIN_ALLOW} is suppressed rather than declared — add a "
            "timer_jobs: row instead"
        )

    def test_declared_timer_units_are_not_reported_as_orphans(self, tmp_path, monkeypatch):
        """The declaration has to actually satisfy the orphan check.

        Removing the allowlist without wiring timer_jobs into orphan_dropins
        would turn morning-signal's drop-in into a permanent finding — trading a
        silent suppression for a permanent alert, which is not an improvement.
        """
        monkeypatch.setattr(cmb, "_DROPIN_ROOT", tmp_path)
        d = tmp_path / "morning-signal.service.d"
        d.mkdir(parents=True)
        (d / "10-memory.conf").write_text("[Service]\nMemoryHigh=600M\nMemoryMax=900M\n")
        spec = self._spec()
        known = ({s["unit"] for s in spec["services"]}
                 | {t["unit"] for t in spec.get("timer_jobs", [])})
        assert cmb.orphan_dropins(known) == []

    def test_timer_caps_fit_the_headroom_the_services_leave(self):
        """The bound itself, against the live steady state measured 2026-07-29.

        Uses a recorded figure rather than reading cgroups so it runs in CI; the
        live version of this check runs on the box every 10 minutes.
        """
        spec = self._spec()
        tj = sum(cmb.parse_bytes(t["memory_max"]) for t in spec.get("timer_jobs", []))
        measured_steady_state_mb = 1207  # box, 2026-07-29, sum of memory.current
        headroom = (int(spec["ram_mb"]) - measured_steady_state_mb) * 1024**2
        assert tj <= headroom, (
            f"timer caps {tj // 1024**2} MB exceed the {headroom // 1024**2} MB "
            "left by the running services — a batch peak that does not fit "
            "evicts a user-facing service (policy section 4, batch-job rule)"
        )


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

    def test_a_declared_timer_job_is_not_an_orphan(self, dropin_root):
        """morning-signal is exempt because it is DECLARED, not allowlisted.

        It used to be excluded by name in `DROPIN_ALLOW`. Since 2026-07-29 it has
        a `timer_jobs:` row instead, and the caller passes those units in — so
        the exemption now comes from a declaration that is itself drift-checked,
        rather than from a hardcoded set nothing re-examines.
        """
        self._dropin(dropin_root, "morning-signal.service", "10-memory.conf",
                     "[Service]\nMemoryHigh=600M\n")
        assert cmb.orphan_dropins({"dashboard.service"}) != [], (
            "with an empty allowlist, an UNDECLARED drop-in must still be caught"
        )
        assert cmb.orphan_dropins({"dashboard.service", "morning-signal.service"}) == []

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


class TestCensoredRequiresACurrentPin:
    """The 2026-08-03 correction, held as its own class so the boundary is
    explicit rather than implied by two fixtures that happen to differ."""

    def test_historical_touch_alone_is_not_censored(self, cgroup_root):
        """metron-api.service as measured live on 2026-08-03: peak 280 == high
        280 after three days up, but current 214 (76%) and provably flat --
        thirteen minutes at a 480M ceiling moved neither current nor peak, with
        zero new MemoryHigh events. Nothing was suppressed, so nothing is a
        floor."""
        _cgroup(cgroup_root, "metron-api.service",
                current=214 * MB, peak=280 * MB, high=280 * MB)
        assert cmb.censored_observation("metron-api.service", WARN) is None

    def test_still_fires_on_every_real_instance_this_check_exists_for(
        self, cgroup_root
    ):
        """The three historical pins, at their measured numbers. A narrowing
        that silences these would be a regression, not a fix."""
        for unit, current, peak, high in [
            ("vires.service", 115, 115, 112),        # 2026-08-03, 103%
            ("dashboard.service", 335, 340, 340),    # 2026-07-31, 98.5%
            ("metron-api.service", 384, 385, 385),   # config-I5216, 99.7%
        ]:
            _cgroup(cgroup_root, unit,
                    current=current * MB, peak=peak * MB, high=high * MB)
            assert cmb.censored_observation(unit, WARN) is not None, unit

    def test_the_boundary_is_the_declared_warn_fraction(self, cgroup_root):
        """Not a second hardcoded threshold: it reuses budget.yaml's
        `headroom_warn_fraction`, the same number that turns a console row
        `attention`."""
        _cgroup(cgroup_root, "just-under.service",
                current=int(0.89 * 280) * MB, peak=280 * MB, high=280 * MB)
        assert cmb.censored_observation("just-under.service", WARN) is None
        _cgroup(cgroup_root, "just-over.service",
                current=int(0.91 * 280) * MB, peak=280 * MB, high=280 * MB)
        assert cmb.censored_observation("just-over.service", WARN) is not None

    def test_an_unreadable_current_still_reports_censored(self, cgroup_root):
        """Fail toward the finding: if the qualifier cannot be evaluated, the
        weaker `peak >= high` tell stands. A narrowing must never turn a
        missing reading into silence."""
        d = _cgroup(cgroup_root, "svc.service",
                    current=200 * MB, peak=280 * MB, high=280 * MB)
        (d / "memory.current").write_text("garbage\n")
        assert cmb.censored_observation("svc.service", WARN) is not None
