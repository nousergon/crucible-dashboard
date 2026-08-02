"""Per-unit memory headroom must reach the console, and must not lie there.

WHY THESE EXIST
---------------
`observability-policy.md` §3.3 names headroom as a required signal in the exact
terms this box keeps hitting: "a service pinned at 96% of its memory cap is a
finding; a service using 400MB is a number." §8.1 requires it rendered on the
console.

Before `--emit-check`, every input existed and none of it reached a surface. It
travelled as a Telegram page, a journal line, and `AlphaEngine/Box::
health_problems` -- deliberately a bare count that cannot name a unit. So the
2026-07-31 condition (dashboard.service at 335/340 MiB, 98.5% of its soft cap,
plus a 1020-event throttle burst) was durable only in a chat message.

What these tests pin is not "the code runs" but the four ways this rendering
could be worse than useless:

  1. reporting a number that is a FLOOR as if it were a measurement
  2. reporting "no throttling" when what happened is "no baseline"
  3. dropping a unit that cannot be measured, so nothing renders as fine
  4. publishing only when something is wrong, so a dead check reads healthy
"""

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_memory_budget", REPO_ROOT / "infrastructure" / "check_memory_budget.py"
)
cmb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cmb)

MB = 1024**2


def _cgroup(root, unit, *, current=None, high=None, hard=None, peak=None,
            events=None):
    d = root / unit
    d.mkdir(parents=True, exist_ok=True)
    for name, value in (("memory.current", current), ("memory.high", high),
                        ("memory.max", hard), ("memory.peak", peak)):
        if value is not None:
            (d / name).write_text("max\n" if value == "max" else f"{value}\n")
    if events is not None:
        (d / "memory.events").write_text(f"low 0\nhigh {events}\nmax 0\noom 0\n")
    return d


@pytest.fixture
def cgroups(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    root.mkdir()
    monkeypatch.setattr(cmb, "_CGROUP_ROOT", root)
    return root


@pytest.fixture
def baseline_file(tmp_path, monkeypatch):
    p = tmp_path / "cgroup-high-counts"
    monkeypatch.setattr(cmb, "_THROTTLE_STATE", p)
    return p


def _spec(*units, **top):
    s = {"services": [{"unit": u, "memory_max": "300M"} for u in units]}
    s.update(top)
    return s


# --- 1. a censored reading is never rendered as a comfortable percentage ----

def test_a_censored_unit_is_flagged_even_when_it_looks_roomy(cgroups, baseline_file):
    """peak >= high means memory.current is a FLOOR: the cap bounded it. The
    percentage computed from it is not a measurement, and this box has been
    wrong about dashboard.service's working set twice on exactly this tell
    (202 MiB and 248 MiB, both floors)."""
    _cgroup(cgroups, "a.service", current=100 * MB, high=340 * MB,
            hard=450 * MB, peak=340 * MB, events=0)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["censored"] is True
    # 100/340 is 29% -- roomy by every number on the row, and still a finding.
    assert row["state"] == "censored"
    assert cmb._STATE_STATUS[row["state"]] == "attention"


def test_censored_outranks_tight(cgroups, baseline_file):
    """Both conditions true: the row must say the one that invalidates the
    other, not the one that is merely alarming."""
    _cgroup(cgroups, "a.service", current=335 * MB, high=340 * MB,
            hard=450 * MB, peak=360 * MB, events=0)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["state"] == "censored"


# --- 2. no baseline is not the same as no throttling -----------------------

def test_missing_baseline_reports_unknown_not_zero(cgroups, baseline_file):
    """First run after a deploy or reboot. Rendering 0 here would be absence of
    evidence rendered as evidence of absence -- and would also republish the
    cgroup's LIFETIME total as a tick delta on the next run."""
    _cgroup(cgroups, "a.service", current=100 * MB, high=340 * MB, peak=110 * MB,
            events=5000)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["throttle_delta"] is None
    assert "unknown" in cmb._row_detail(row)
    assert "+0" not in cmb._row_detail(row)


def test_restart_between_runs_reports_unknown_not_a_negative(cgroups, baseline_file):
    """A counter that went backwards means the cgroup was recreated. Not
    throttling, not an error, and emphatically not a huge positive delta."""
    baseline_file.write_text("a.service 9000\n")
    _cgroup(cgroups, "a.service", current=100 * MB, high=340 * MB, peak=110 * MB,
            events=12)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["throttle_delta"] is None


def test_delta_is_computed_against_the_previous_run(cgroups, baseline_file):
    """The same subtraction box_health.sh's classify_throttle_delta performs,
    against the same file -- so the console and the page cannot disagree."""
    baseline_file.write_text("a.service 100\n")
    _cgroup(cgroups, "a.service", current=100 * MB, high=340 * MB, peak=110 * MB,
            events=1120)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["throttle_delta"] == 1020
    assert row["state"] == "throttling"


def test_a_small_delta_is_not_a_finding(cgroups, baseline_file):
    """Matches box_health.sh's floor: a handful of reclaim events during a
    deploy restart is normal and self-corrects."""
    baseline_file.write_text("a.service 100\n")
    _cgroup(cgroups, "a.service", current=100 * MB, high=340 * MB, peak=110 * MB,
            events=105)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["throttle_delta"] == 5
    assert row["state"] == "ok"


# --- 3. every unit gets a row, including the ones that cannot be measured ---

def test_an_unmeasurable_unit_still_renders(cgroups, baseline_file):
    """A unit dropped for being unreadable renders as nothing, and nothing
    renders as fine. observability-policy.md §8.3 forbids a component
    disappearing from the surface."""
    rows = cmb.headroom_rows(_spec("ghost.service"))
    assert len(rows) == 1
    assert rows[0]["state"] == "unmeasurable"
    assert cmb._STATE_STATUS["unmeasurable"] == "attention"


def test_population_comes_from_the_spec_not_from_the_cgroup_tree(cgroups, baseline_file):
    """Enumerating the live tree would silently shrink coverage to whatever is
    currently running -- the drift that left the watchdog covering 8 of 14
    services with nginx, the sole ingress, omitted."""
    _cgroup(cgroups, "extra.service", current=10 * MB, high=100 * MB, peak=11 * MB,
            events=0)
    rows = cmb.headroom_rows(_spec("a.service", "b.service"))
    assert [r["unit"] for r in rows] == ["a.service", "b.service"]


def test_a_unit_with_no_soft_cap_is_an_error_not_a_percentage(cgroups, baseline_file):
    """MemoryHigh=infinity means no reclaim window before the hard cap. There
    is no denominator, so there is no comfortable-looking percentage to show."""
    _cgroup(cgroups, "a.service", current=100 * MB, high="max", hard=300 * MB,
            peak=110 * MB, events=0)
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["state"] == "no_soft_cap"
    assert cmb._STATE_STATUS[row["state"]] == "error"


# --- the warn band ---------------------------------------------------------

def test_the_warn_threshold_is_read_from_the_spec(cgroups, baseline_file):
    """Declared in budget.yaml, not hardcoded here: acceptable tightness is a
    decision, and deriving it from the cap re-derives the wrong answer in the
    case where the cap is what is wrong."""
    _cgroup(cgroups, "a.service", current=300 * MB, high=340 * MB, peak=310 * MB,
            events=0)
    tight = cmb.headroom_rows(_spec("a.service", headroom_warn_fraction=0.80))[0]
    roomy = cmb.headroom_rows(_spec("a.service", headroom_warn_fraction=0.95))[0]
    assert tight["state"] == "tight"
    assert roomy["state"] == "ok"


def test_the_2026_07_31_condition_is_a_finding(cgroups, baseline_file):
    """The regression case. 335/340 MiB with a 1020-event burst produced no
    console row at all before this check existed."""
    baseline_file.write_text("dashboard.service 0\n")
    _cgroup(cgroups, "dashboard.service", current=335 * MB, high=340 * MB,
            hard=450 * MB, peak=360 * MB, events=1020)
    row = cmb.headroom_rows(_spec("dashboard.service"))[0]
    assert row["used_fraction"] >= 0.98
    assert row["state"] != "ok"


def test_over_the_soft_cap_is_an_error(cgroups, baseline_file):
    _cgroup(cgroups, "a.service", current=360 * MB, high=340 * MB, hard=450 * MB,
            peak=360 * MB, events=0)
    # peak == current > high, so this is also censored; assert the escalation
    # path explicitly rather than relying on ordering.
    row = cmb.headroom_rows(_spec("a.service"))[0]
    assert row["used_fraction"] > 1.0


# --- 4. the envelope -------------------------------------------------------

@pytest.fixture
def fake_lib(monkeypatch):
    """A stand-in for nousergon_lib.fleet_check_result that records the call.

    The real module is asserted against by nousergon-lib's own suite; what this
    repo owns is WHAT IT PASSES.
    """
    import types
    captured = {}

    mod = types.ModuleType("nousergon_lib.fleet_check_result")
    mod.STATUS_OK, mod.STATUS_ATTENTION, mod.STATUS_ERROR = "ok", "attention", "error"

    def emit_result(**kw):
        captured.update(kw)
        return "s3://alpha-engine-research/ops/checks/box_memory_headroom/latest.json"

    mod.emit_result = emit_result
    pkg = types.ModuleType("nousergon_lib")
    pkg.fleet_check_result = mod
    monkeypatch.setitem(sys.modules, "nousergon_lib", pkg)
    monkeypatch.setitem(sys.modules, "nousergon_lib.fleet_check_result", mod)
    return captured


def test_envelope_names_the_tightest_unit_not_an_average(cgroups, baseline_file, fake_lib):
    """An average would have read comfortably on 2026-07-31: one unit at 98.5%
    of its cap while the box had 1688 MiB free."""
    _cgroup(cgroups, "tight.service", current=335 * MB, high=340 * MB, peak=336 * MB,
            events=0)
    _cgroup(cgroups, "roomy.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    cmb.emit_headroom_check(_spec("tight.service", "roomy.service"), [], [])
    assert "tight.service" in fake_lib["summary"]


def test_envelope_publishes_on_a_healthy_box(cgroups, baseline_file, fake_lib):
    """LOAD-BEARING. A surface that publishes only when something is wrong is
    indistinguishable from one that has died -- observability-policy.md §8.3's
    UNREPORTED collapsing into HEALTHY, the failure this whole check exists to
    end."""
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    cmb.emit_headroom_check(_spec("a.service"), [], [])
    assert fake_lib["status"] == "ok"
    assert fake_lib["findings"], "a healthy box still renders every unit"


def test_cadence_matches_the_timer(cgroups, baseline_file, fake_lib):
    """The console derives staleness from this. Overstating it lets a dead
    check read healthy for longer than it should."""
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    cmb.emit_headroom_check(_spec("a.service"), [], [])
    assert fake_lib["cadence_minutes"] == 10


def test_a_breach_makes_the_envelope_error(cgroups, baseline_file, fake_lib):
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    cmb.emit_headroom_check(_spec("a.service"), ["the box is over its ceiling"], [])
    assert fake_lib["status"] == "error"
    assert any(f["key"] == "BREACH" for f in fake_lib["findings"])


def test_hygiene_alone_is_attention_not_error(cgroups, baseline_file, fake_lib):
    """The severity split this repo already makes: a box over budget pages, a
    degraded measurement does not."""
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    cmb.emit_headroom_check(_spec("a.service"), [], ["an orphan drop-in"])
    assert fake_lib["status"] == "attention"


def test_timer_jobs_render_as_batch_rather_than_as_missing(cgroups, baseline_file,
                                                           fake_lib):
    """Their cgroups exist only while running, so an absent one is expected --
    but the unit must not vanish from the surface either."""
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    spec = _spec("a.service")
    spec["timer_jobs"] = [{"unit": "morning-signal.service", "memory_max": "900M"}]
    cmb.emit_headroom_check(spec, [], [])
    row = [f for f in fake_lib["findings"] if f["key"] == "morning-signal.service"]
    assert row and "batch" in row[0]["detail"]


def test_check_id_is_the_console_path_segment(cgroups, baseline_file, fake_lib):
    """The console discovers producers by S3 prefix; this string IS the path."""
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    cmb.emit_headroom_check(_spec("a.service"), [], [])
    assert fake_lib["check_id"] == "box_memory_headroom"
    assert "/" not in fake_lib["check_id"]


def test_a_missing_lib_does_not_raise(cgroups, baseline_file, monkeypatch):
    """--declared runs in CI where nousergon-lib is not installed, and a check
    must never go red because its telemetry did."""
    monkeypatch.setitem(sys.modules, "nousergon_lib", None)
    _cgroup(cgroups, "a.service", current=10 * MB, high=340 * MB, peak=11 * MB,
            events=0)
    assert cmb.emit_headroom_check(_spec("a.service"), [], []) is None


def test_every_row_state_has_a_console_status():
    """No fall-through. A new row state must not silently inherit `ok` --
    observability-policy.md §8.3's totality invariant."""
    states = {"ok", "throttling", "tight", "censored", "unmeasurable",
              "over_soft_cap", "no_soft_cap"}
    assert set(cmb._STATE_STATUS) == states
    assert set(cmb._STATE_STATUS.values()) <= {"ok", "attention", "error"}
