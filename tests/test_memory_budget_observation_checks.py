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

import datetime as _dt
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


def _cgroup(tmp_path, unit, *, current=None, peak=None, high=None,
            anon=None, file_cache=0, swap=None):
    """Build a fake cgroup v2 tree for one unit."""
    d = tmp_path / unit
    d.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("memory.current", current),
        ("memory.peak", peak),
        ("memory.high", high),
        ("memory.swap.current", swap),
    ):
        if value is not None:
            (d / name).write_text("max\n" if value == "max" else f"{value}\n")
    # Written only when a test asks for it: absent memory.stat is the state
    # every pre-2026-08-16 case was written against, and the code must keep its
    # verdict when the table cannot be read.
    if anon is not None:
        (d / "memory.stat").write_text(
            f"anon {anon}\nfile {file_cache}\nkernel_stack 0\n"
        )
    return d


@pytest.fixture
def cgroup_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cmb, "_CGROUP_ROOT", tmp_path)
    return tmp_path


# budget.yaml's `headroom_warn_fraction`. Passed explicitly rather than
# defaulted in the function, so a change to the declared value cannot silently
# diverge from what these tests assert.
WARN = 0.90


class TestOverProvisioned:
    """The direction no check on this box has ever reported.

    Twenty-two commits have touched budget.yaml and every one that changed a
    number raised it, so sum(memory_max) reached 4324 MB against a 4373 MB
    bound while the sum of real peaks was 2254 MB — 69% of the ceiling. The
    budget read full; the box was not.
    """

    def _c(self, root, unit, *, peak, mx):
        d = root / unit
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.peak").write_text(f"{peak}\n")
        (d / "memory.max").write_text(f"{mx}\n")
        return d

    def test_a_long_observed_cap_far_above_its_peak_is_reported(self, cgroup_root):
        """llm-egress-proxy's live shape: 127 MiB peak, 400 MiB cap, 16 days."""
        self._c(cgroup_root, "llm-egress-proxy.service", peak=127 * MB, mx=400 * MB)
        msg = cmb.over_provisioned("llm-egress-proxy.service", 16.0)
        assert msg is not None
        assert "OVER-PROVISIONED" in msg
        assert "READ THIS UNIT'S budget.yaml NOTE FIRST" in msg

    def test_a_short_uptime_is_never_reported(self, cgroup_root):
        """THE safety property. vires: 87 MiB peak against 460 MiB looks like
        373 MB of slack, and is not — its ONNX vector leg lazy-loads to ~316
        MiB on the first exercise search, which had not run since the restart.
        Trimming on that reading reproduces the 2026-08-03 wedge."""
        self._c(cgroup_root, "vires.service", peak=87 * MB, mx=460 * MB)
        assert cmb.over_provisioned("vires.service", 1.2) is None

    def test_uptime_exactly_at_the_floor_is_reported(self, cgroup_root):
        self._c(cgroup_root, "svc.service", peak=40 * MB, mx=250 * MB)
        assert cmb.over_provisioned("svc.service", 7.0) is not None

    def test_unknown_uptime_is_never_reported(self, cgroup_root):
        """No window means no argument for a lower cap. Fails toward silence,
        which is the safe direction for a check that proposes taking memory
        away."""
        self._c(cgroup_root, "svc.service", peak=40 * MB, mx=250 * MB)
        assert cmb.over_provisioned("svc.service", None) is None

    def test_a_well_sized_cap_is_silent(self, cgroup_root):
        """litellm-proxy: 296 MiB peak, 500 MiB cap = 1.7x. Under the 2.5x
        ratio, because memory_max is a spike ceiling and these units burst."""
        self._c(cgroup_root, "litellm-proxy.service", peak=296 * MB, mx=500 * MB)
        assert cmb.over_provisioned("litellm-proxy.service", 30.0) is None

    def test_an_uncapped_unit_is_silent(self, cgroup_root):
        d = cgroup_root / "svc.service"
        d.mkdir(parents=True)
        (d / "memory.peak").write_text(f"{40 * MB}\n")
        (d / "memory.max").write_text("max\n")
        assert cmb.over_provisioned("svc.service", 30.0) is None

    def test_a_zero_peak_is_silent(self, cgroup_root):
        """Guards the ratio against division by zero, and a unit that has
        allocated nothing has not been observed."""
        self._c(cgroup_root, "svc.service", peak=0, mx=250 * MB)
        assert cmb.over_provisioned("svc.service", 30.0) is None

    def test_absent_cgroup_is_silent(self, cgroup_root):
        assert cmb.over_provisioned("absent.service", 30.0) is None


class TestApproachingTheCap:
    """The window in which raising a soft cap is still free.

    Once a unit pins, memory.current is a floor and the raise has to be sized
    to a number nobody trusts — that guess is how nousergon-console was raised
    to "~2x the censored floor" on 2026-08-11 and re-pinned inside a day. Before
    it pins, the reading is real and only memory_high has to move, so it costs
    nothing against sum(memory_max).
    """

    def test_the_crucible_dash_api_shape_is_reported(self, cgroup_root):
        """The exact live reading on 2026-08-12, which nothing reported."""
        _cgroup(cgroup_root, "crucible-dash-api.service",
                current=244 * MB, peak=244 * MB, high=245 * MB)
        msg = cmb.approaching_the_cap("crucible-dash-api.service", WARN)
        assert msg is not None
        assert "APPROACHING" in msg
        assert "memory_max does not need to move" in msg

    def test_a_unit_already_pinned_is_left_to_the_censored_check(self, cgroup_root):
        """One condition, one voice — the two must never both fire."""
        _cgroup(cgroup_root, "nousergon-console.service",
                current=160 * MB, peak=160 * MB, high=160 * MB)
        assert cmb.approaching_the_cap("nousergon-console.service", WARN) is None
        assert cmb.censored_observation("nousergon-console.service", WARN) is not None

    def test_comfortably_below_the_band_is_silent(self, cgroup_root):
        _cgroup(cgroup_root, "vires.service",
                current=87 * MB, peak=87 * MB, high=380 * MB)
        assert cmb.approaching_the_cap("vires.service", WARN) is None

    def test_a_historical_graze_without_a_current_pin_is_silent(self, cgroup_root):
        """memory.peak never decays short of a restart, so without the
        current-pin requirement a single graze would report forever. This is
        the metron-api false positive that shaped the censored check, one band
        lower."""
        _cgroup(cgroup_root, "metron-api.service",
                current=150 * MB, peak=265 * MB, high=280 * MB)
        assert cmb.approaching_the_cap("metron-api.service", WARN) is None

    def test_infinite_high_is_silent(self, cgroup_root):
        _cgroup(cgroup_root, "svc.service",
                current=100 * MB, peak=100 * MB, high="max")
        assert cmb.approaching_the_cap("svc.service", WARN) is None

    def test_absent_cgroup_is_silent(self, cgroup_root):
        assert cmb.approaching_the_cap("absent.service", WARN) is None


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


class TestTheBoundMeasuresTheWorkingSet:
    """Brian ruling 2026-08-16 (alpha-engine-config-I7449, option (c)).

    The bound used to key on `sum(memory.current)`, which charges reclaimable
    page cache. Measured on the box with warm caches that day: 1795 MB charged,
    ~375 MB of it cache, and `dashboard.service` alone holding 224 MiB of cache
    against a 183 MiB working set. Cache is returned under pressure by design,
    so a bound keyed on it trips on a condition that is not the failure the
    bound exists to prevent.

    The constant moved with the basis (0.60 -> 0.50), derived to hold the
    absolute headroom unchanged at the switch. These tests pin the pair: a
    basis change without the constant would have loosened the invariant by ~10
    points of RAM as a side effect of a reporting change.
    """

    def test_the_declared_constant_matches_its_derivation(self):
        import yaml
        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        ram = int(spec["ram_mb"])
        old_limit = 0.60 * ram              # the bound before the switch
        warm_charge_mb = 1795               # box, 2026-08-16, warm caches
        warm_working_set_mb = 1420          # same reading, anon over budgeted units
        headroom_preserved = warm_working_set_mb + (old_limit - warm_charge_mb)
        assert round(headroom_preserved / ram, 2) == float(
            spec["max_steady_state_fraction"]
        ) == 0.50

    def test_cache_no_longer_counts_toward_the_bound(self, cgroup_root):
        """The whole point. A unit charged 400 MB of which 100 MB is working
        set contributes 100 MB, not 400 MB."""
        _cgroup(cgroup_root, "a.service", current=400 * MB, peak=410 * MB,
                high=420 * MB, anon=100 * MB, file_cache=300 * MB)
        assert cmb.working_set_total_mb(["a.service"]) == (100, [])
        # The charge is still measured — it is simply not what the bound keys on.
        assert cmb.steady_state_mb(["a.service"], WARN)[0] == 400

    def test_swap_counts_toward_the_bound(self, cgroup_root):
        """An evicted page is working set that was pushed out. Excluding it
        would let a swapping box read as comfortable, which is the one state
        where the bound most needs to be honest."""
        _cgroup(cgroup_root, "a.service", current=100 * MB, peak=110 * MB,
                high=420 * MB, anon=100 * MB, swap=200 * MB)
        assert cmb.working_set_total_mb(["a.service"]) == (300, [])

    def test_an_unreadable_unit_leaves_the_bound_unproven(self, cgroup_root):
        """A sum missing a unit understates, and understating is the direction
        that reads as safe. It must be named rather than silently dropped."""
        _cgroup(cgroup_root, "a.service", current=100 * MB, peak=10 * MB,
                high=400 * MB, anon=90 * MB)
        _cgroup(cgroup_root, "b.service", current=100 * MB, peak=10 * MB,
                high=400 * MB)
        assert cmb.working_set_total_mb(["a.service", "b.service"]) == (
            90, ["b.service"]
        )


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


# ── alpha-engine-config-I6277: a --runtime override outranks the /etc drop-in ─
#
# WHY THESE EXIST
# ----------------
# `systemctl set-property --runtime <unit> MemoryMax=...` writes
# /run/systemd/system.control/<unit>.d/50-Memory{High,Max}.conf, which
# OUTRANKS /etc/systemd/system/<unit>.d/99-resource-limits.conf. Before this,
# orphan_dropins() only ever globbed _DROPIN_ROOT (/etc), so this whole
# mechanism was invisible to the check, and `daemon-reload` -- the only
# remedy install-resource-limits.sh knows -- does not touch /run at all.
# Measured live on metron-api.service, 2026-08-03 17:11-17:40 UTC: effective
# MemoryHigh/MemoryMax 480M/560M against a declared 280M/350M, still live and
# paging 30 minutes after the installer had supposedly fixed it.

class TestRuntimeDropinOverride:
    @pytest.fixture
    def runtime_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cmb, "_RUNTIME_DROPIN_ROOT", tmp_path)
        return tmp_path

    def _write(self, root, unit, name, body):
        d = root / f"{unit}.d"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)

    def test_absent_directory_is_normal_not_a_finding(self, runtime_root):
        """Root-owned and cleared on reboot -- most units, most of the time,
        have no live override. Absence must not raise or report anything."""
        assert cmb.runtime_dropin_overrides("metron-api.service") == []

    def test_a_live_memory_max_override_is_named(self, runtime_root):
        self._write(runtime_root, "metron-api.service", "50-MemoryMax.conf",
                     "[Service]\nMemoryMax=560M\n")
        found = cmb.runtime_dropin_overrides("metron-api.service")
        assert len(found) == 1
        assert found[0].endswith("50-MemoryMax.conf")

    def test_a_live_memory_high_override_is_also_named(self, runtime_root):
        self._write(runtime_root, "metron-api.service", "50-MemoryHigh.conf",
                     "[Service]\nMemoryHigh=480M\n")
        assert cmb.runtime_dropin_overrides("metron-api.service") != []

    def test_a_dropin_without_memory_settings_is_ignored(self, runtime_root):
        """This mechanism owns memory overrides only, not every --runtime
        property ever set on the unit."""
        self._write(runtime_root, "metron-api.service", "50-Other.conf",
                     "[Service]\nEnvironment=FOO=bar\n")
        assert cmb.runtime_dropin_overrides("metron-api.service") == []

    def test_a_different_units_override_is_not_attributed_here(self, runtime_root):
        self._write(runtime_root, "vires.service", "50-MemoryMax.conf",
                     "[Service]\nMemoryMax=460M\n")
        assert cmb.runtime_dropin_overrides("metron-api.service") == []


class TestRuntimeOverrideAttachesToMainsDriftBreach:
    """The end-to-end contract: --installed must NAME the mechanism, the
    path, and the exact revert command, on the SAME line as the existing
    MemoryMax drift breach -- not as a second, uncorrelated finding."""

    def _fixture_budget(self, tmp_path, *, extra_service_yaml: str = ""):
        budget = tmp_path / "budget.yaml"
        budget.write_text(
            "ram_mb: 2000\n"
            "reserve_fraction: 0.15\n"
            "max_overcommit_ratio: 2.0\n"
            "max_steady_state_fraction: 0.60\n"
            "services:\n"
            "  - unit: metron-api.service\n"
            "    memory_high: 280M\n"
            "    memory_max: 350M\n"
            f"{extra_service_yaml}"
        )
        return budget

    def _run_installed(self, tmp_path, monkeypatch, budget,
                        have_max="560M", have_high="480M"):
        import sys

        monkeypatch.setattr(cmb, "BUDGET", budget)
        monkeypatch.setattr(
            cmb, "systemd_show",
            lambda unit, prop: {"MemoryMax": have_max,
                                 "MemoryHigh": have_high,
                                 "ActiveEnterTimestampMonotonic": "0"}[prop],
        )
        monkeypatch.setattr(cmb, "ram_mb_from_proc", lambda: 2000)
        monkeypatch.setattr(cmb, "_CGROUP_ROOT", tmp_path / "cgroup")
        monkeypatch.setattr(cmb, "_DROPIN_ROOT", tmp_path / "etc")
        monkeypatch.setattr(cmb, "_THROTTLE_STATE", tmp_path / "throttle-state")
        argv = sys.argv
        sys.argv = ["check_memory_budget.py", "--installed"]
        try:
            return cmb.main()
        finally:
            sys.argv = argv

    def test_override_is_named_on_the_drift_breach_and_still_pages(
        self, tmp_path, monkeypatch, capsys
    ):
        budget = self._fixture_budget(tmp_path)
        runtime_root = tmp_path / "runtime"
        (runtime_root / "metron-api.service.d").mkdir(parents=True)
        (runtime_root / "metron-api.service.d" / "50-MemoryMax.conf").write_text(
            "[Service]\nMemoryMax=560M\n"
        )
        monkeypatch.setattr(cmb, "_RUNTIME_DROPIN_ROOT", runtime_root)

        rc = self._run_installed(tmp_path, monkeypatch, budget)
        err = capsys.readouterr().err

        # No uncensor_until declared -- an undeclared override still pages.
        assert rc == 1
        assert "metron-api.service: MemoryMax drift" in err
        assert "LIVE OVERRIDE" in err
        assert "systemctl set-property --runtime" in err
        assert str(runtime_root / "metron-api.service.d" / "50-MemoryMax.conf") in err
        assert "systemctl revert metron-api.service" in err
        assert "install-resource-limits.sh will NOT fix this" in err
        # Attached to the SAME line as the drift finding, not a second one.
        assert err.count("MemoryMax drift") == 1

    def test_no_override_present_omits_the_mechanism_text(
        self, tmp_path, monkeypatch, capsys
    ):
        """The drift breach must still fire on an ordinary hand-edited /etc
        drop-in that carries no /run override at all -- this file must not
        start requiring the mechanism to be present to report drift."""
        budget = self._fixture_budget(tmp_path)
        monkeypatch.setattr(cmb, "_RUNTIME_DROPIN_ROOT", tmp_path / "runtime-empty")

        rc = self._run_installed(tmp_path, monkeypatch, budget)
        err = capsys.readouterr().err

        assert rc == 1
        assert "metron-api.service: MemoryMax drift" in err
        assert "LIVE OVERRIDE" not in err


class TestUncensorMeasurementWindow:
    """alpha-engine-config-I6277 deliverable 3: an optional, declared,
    time-boxed exemption for the documented un-censoring procedure
    (config-I6263) -- so it stops paging every 10 minutes for the duration of
    a deliberate measurement, without becoming a way to silence a real drift
    permanently. Absence of the key, or a deadline already passed, is not a
    window: an abandoned measurement gets LOUDER, never quieter."""

    def test_no_key_is_not_active(self):
        assert cmb.uncensor_deadline({}) is None
        assert cmb.uncensor_active({}) is False

    def test_a_future_deadline_is_active(self):
        future = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(days=1)).isoformat()
        svc = {"uncensor_until": future}
        assert cmb.uncensor_deadline(svc) is not None
        assert cmb.uncensor_active(svc) is True

    def test_a_past_deadline_is_not_active(self):
        past = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(days=1)).isoformat()
        svc = {"uncensor_until": past}
        assert cmb.uncensor_active(svc) is False

    def test_a_malformed_deadline_fails_toward_the_loud_outcome(self):
        """A typo must not silently disable the window (that would be
        indistinguishable from an active one at the wrong severity); it must
        also not crash the check. It reads as absent -- ordinary breach."""
        assert cmb.uncensor_deadline({"uncensor_until": "not-a-date"}) is None
        assert cmb.uncensor_active({"uncensor_until": "not-a-date"}) is False

    def _budget_with_window(self, tmp_path, uncensor_until=None):
        extra = f"    uncensor_until: '{uncensor_until}'\n" if uncensor_until else ""
        budget = tmp_path / "budget.yaml"
        budget.write_text(
            "ram_mb: 2000\n"
            "reserve_fraction: 0.15\n"
            "max_overcommit_ratio: 2.0\n"
            "max_steady_state_fraction: 0.60\n"
            "services:\n"
            "  - unit: metron-api.service\n"
            "    memory_high: 280M\n"
            "    memory_max: 350M\n"
            f"{extra}"
        )
        return budget

    def _run_installed(self, tmp_path, monkeypatch, budget):
        import sys

        monkeypatch.setattr(cmb, "BUDGET", budget)
        monkeypatch.setattr(
            cmb, "systemd_show",
            lambda unit, prop: {"MemoryMax": "560M", "MemoryHigh": "480M",
                                "ActiveEnterTimestampMonotonic": "0"}[prop],
        )
        monkeypatch.setattr(cmb, "ram_mb_from_proc", lambda: 2000)
        monkeypatch.setattr(cmb, "_CGROUP_ROOT", tmp_path / "cgroup")
        monkeypatch.setattr(cmb, "_DROPIN_ROOT", tmp_path / "etc")
        monkeypatch.setattr(cmb, "_RUNTIME_DROPIN_ROOT", tmp_path / "runtime-empty")
        monkeypatch.setattr(cmb, "_THROTTLE_STATE", tmp_path / "throttle-state")
        argv = sys.argv
        sys.argv = ["check_memory_budget.py", "--installed"]
        try:
            return cmb.main()
        finally:
            sys.argv = argv

    def test_same_input_is_hygiene_inside_the_window_and_breach_after(
        self, tmp_path, monkeypatch, capsys
    ):
        """closes-when: identical live and declared values -- only the
        deadline differs -- must yield rc=2 while the window is open and
        rc=1 once it passes."""
        future = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(days=1)).isoformat()
        rc_inside = self._run_installed(
            tmp_path, monkeypatch, self._budget_with_window(tmp_path, future)
        )
        err_inside = capsys.readouterr().err
        assert rc_inside == 2
        assert "metron-api.service: MemoryMax drift" in err_inside
        assert "HYGIENE:" in err_inside
        assert "BREACH:" not in err_inside
        assert "uncensor_until window" in err_inside
        assert future.split(".")[0] in err_inside or future in err_inside

        past = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(days=1)).isoformat()
        rc_after = self._run_installed(
            tmp_path, monkeypatch, self._budget_with_window(tmp_path, past)
        )
        err_after = capsys.readouterr().err
        assert rc_after == 1
        assert "metron-api.service: MemoryMax drift" in err_after
        assert "BREACH:" in err_after
        assert "has PASSED" in err_after

    def test_no_window_declared_is_an_ordinary_breach(
        self, tmp_path, monkeypatch, capsys
    ):
        rc = self._run_installed(
            tmp_path, monkeypatch, self._budget_with_window(tmp_path, None)
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "BREACH:" in err
        assert "uncensor_until" not in err

    def test_aggregate_ratio_contribution_is_credited_back_inside_the_window(
        self, tmp_path, monkeypatch, capsys
    ):
        """Deliverable 3's second half: the unit's contribution to the
        AGGREGATE overcommit bound is also credited back to its declared cap
        while the window is open, so a deliberate measurement on one service
        cannot also trip the box-wide ratio bound as a page.

        Two services, ratio chosen so the RAW live sum (560 + 300 = 860 MB)
        exceeds a 700 MB allowed sum but the CREDITED sum (metron-api capped
        at its declared 350M while its window is open: 350 + 300 = 650 MB)
        does not -- the aggregate bound must follow the credited sum, not
        the raw one, and say so rather than silently pass.
        """
        budget = tmp_path / "budget.yaml"
        future = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(days=1)).isoformat()
        budget.write_text(
            "ram_mb: 700\n"
            "reserve_fraction: 0.0\n"
            "max_overcommit_ratio: 1.0\n"
            "max_steady_state_fraction: 0.90\n"
            "services:\n"
            "  - unit: metron-api.service\n"
            "    memory_high: 280M\n"
            "    memory_max: 350M\n"
            f"    uncensor_until: '{future}'\n"
            "  - unit: other-svc.service\n"
            "    memory_high: 250M\n"
            "    memory_max: 300M\n"
        )
        live = {
            "metron-api.service": {"MemoryMax": "560M", "MemoryHigh": "480M",
                                   "ActiveEnterTimestampMonotonic": "0"},
            "other-svc.service": {"MemoryMax": "300M", "MemoryHigh": "250M",
                                  "ActiveEnterTimestampMonotonic": "0"},
        }
        monkeypatch.setattr(cmb, "BUDGET", budget)
        monkeypatch.setattr(cmb, "systemd_show",
                             lambda unit, prop: live[unit][prop])
        monkeypatch.setattr(cmb, "ram_mb_from_proc", lambda: 700)
        monkeypatch.setattr(cmb, "_CGROUP_ROOT", tmp_path / "cgroup")
        monkeypatch.setattr(cmb, "_DROPIN_ROOT", tmp_path / "etc")
        monkeypatch.setattr(cmb, "_RUNTIME_DROPIN_ROOT", tmp_path / "runtime-empty")
        monkeypatch.setattr(cmb, "_THROTTLE_STATE", tmp_path / "throttle-state")
        import sys
        argv = sys.argv
        sys.argv = ["check_memory_budget.py", "--installed"]
        try:
            rc = cmb.main()
        finally:
            sys.argv = argv
        err = capsys.readouterr().err

        # Neither unit's declared/live pair alone breaches the aggregate
        # bound (other-svc has no drift at all); only the RAW sum does.
        assert rc == 2, err
        assert "BREACH:" not in err
        assert "OVER ONLY because" in err
        assert "credited back" in err
        assert "650 MB" in err  # the credited sum
        assert "860 MB" in err  # the raw sum, still shown
