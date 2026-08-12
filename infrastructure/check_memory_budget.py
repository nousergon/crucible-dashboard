#!/usr/bin/env python3
"""Enforce the dashboard box's aggregate memory budget.

The invariant: the sum of every application unit's MemoryMax must fit in RAM
with a reserve left over for the kernel and page cache.

Per-service caps bound ONE runaway process. They do nothing when several
services grow toward their individually-legal ceilings at once -- the kernel
OOM killer then fires against whatever it picks, which need not be the service
that caused the pressure. On 2026-07-27 the sum stood at ~4650 MB against
1913 MB of RAM (2.4x) and the box cascaded into an unmanageable state.

Two modes:

  --declared   Check budget.yaml against itself (no systemd needed). This is
               the CI mode: it catches a service being added to the budget
               that pushes the sum over the ceiling, before it ever ships.

  --installed  Check what systemd has ACTUALLY loaded, and additionally assert
               that every unit in budget.yaml is installed with the values the
               budget declares. This is the on-box mode, run by box_health.sh.
               It catches drift -- a hand-edited drop-in, a unit whose limits
               were never installed, or a service running with no cap at all.

STEADY STATE IS MEASURED, NEVER DECLARED
----------------------------------------
`max_steady_state_fraction` is the bound that governs normal operation. Until
2026-07-29 it was computed by summing a hand-maintained `observed_mb:` per
service in budget.yaml -- a fixed number describing a continuously-moving
quantity. It went wrong the only way it could: three separate hand
re-measurements (litellm-proxy, metron-api/config-I5216,
dashboard/config-I5237), and on the day the check that compares them to the box
first ran, three of fourteen units disagreed with their declaration, one by
2.8x. Each round produced a true, unactionable finding whose only remedy was
editing the number back.

A declared value the live system can measure is not a declaration, it is a
cache -- and an uninvalidated cache generates a permanent stream of correct
alerts about itself. `observed_mb` is therefore GONE. In --installed mode the
steady-state sum is read from each unit's `memory.current`. budget.yaml now
declares only the BOUND (`max_steady_state_fraction`), which is a policy
decision a human legitimately owns.

Consequence, stated rather than hidden: the steady-state bound cannot be
evaluated off-box, so --declared no longer checks it. That is honest -- the CI
version of this bound was only ever as good as numbers nobody could verify --
and the loss is covered by box_health.sh running --installed every 10 minutes.

SEVERITY IS A PROPERTY OF THE INVARIANT, NOT OF THIS CHECK
----------------------------------------------------------
Two findings that are not the same event must not exit the same way. A box over
its memory budget is a page; a censored reading or an unowned drop-in is
bookkeeping about how well we can see the box. Emitting both at one severity
makes the page rate track hygiene rather than health, which is how a class gets
tuned out. Hence:

  0  every invariant holds and nothing impairs the reading
  1  INVARIANT BREACH -- caps over the declared overcommit, steady state over
     its limit, RAM drift, a cap that does not match the budget, a service with
     no cap or no reclaim window. Pages.
  2  OBSERVATION HYGIENE ONLY -- every invariant above holds; something is
     degrading the measurement (censored reading, unmeasurable unit, orphan
     drop-in). Recorded, does not page.
  3  the check could not run at all (watchdog malfunction).

Policy: nous-ergon-ops/policies/shared-application-host-policy.md T1-1;
severity tiering per overseer-policy.md invariant 17.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import pathlib
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced loudly, never swallowed
    print("check_memory_budget: PyYAML not available", file=sys.stderr)
    sys.exit(3)  # cannot run -- a watchdog malfunction, not a budget verdict

BUDGET = pathlib.Path(__file__).parent / "systemd" / "resource-limits" / "budget.yaml"

# Module constants rather than literals inside the readers, so tests can point
# them at a fixture tree. A check that can only be exercised on the live box is
# a check that never gets exercised.
_CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup/system.slice")

_SUFFIX = {"K": 1024, "M": 1024**2, "G": 1024**3}

# ── The console surface (alpha-engine-config-I5863) ────────────────────────
#
# observability-policy.md §3.3 requires headroom as a signal in its own right:
# "memory and disk against their caps, and cgroup throttling where a cap exists.
# A service pinned at 96% of its memory cap is a finding; a service using 400MB
# is a number." §8.1 requires it RENDERED, and until this existed none of it
# reached a console surface -- it travelled as a Telegram page and a journal
# line, plus AlphaEngine/Box::health_problems, which is deliberately a bare
# count and cannot name a unit.
#
# This does NOT re-implement any detection. Every input below is already
# computed by this file or by box_health.sh; --emit-check renders their verdict
# onto the console surface. observability-policy.md §8.2: "a new CONSUMER
# renders the existing planes' verdicts; it never re-implements a fifth monitor
# asking a question one of the four already answers."
CHECK_ID = "box_memory_headroom"
CHECK_LABEL = "Dashboard box memory headroom (per-service caps + throttling)"
# box-health.timer fires every 10 minutes and this publishes once per run. The
# console derives staleness from this, so it has to be the REAL cadence: too
# high and a dead check reads healthy for longer than it should.
CHECK_CADENCE_MINUTES = 10

# The counters box_health.sh recorded at the END of its previous run. Reading
# the same file rather than keeping a second one is the point -- the delta this
# check reports and the delta box_health.sh alerts on are then the same number
# by construction, not by two implementations agreeing.
#
# $STATE_DIRECTORY is exported by systemd from box-health.service's
# StateDirectory=; the literal fallback is for running this by hand.
_THROTTLE_STATE = pathlib.Path(
    os.environ.get("STATE_DIRECTORY", "/var/lib/box-health")
) / "cgroup-high-counts"

# Matches box_health.sh's CGROUP_HIGH_DELTA_MIN. A handful of reclaim events
# during a deploy restart is normal and self-corrects; the at-rest rate on this
# box is zero, so 10 inside one tick is unambiguously a burst.
THROTTLE_DELTA_MIN = 10


def parse_bytes(value: str) -> int:
    """Parse a systemd size string (250M, 1G, 1048576) into bytes."""
    value = str(value).strip()
    if value in ("infinity", ""):
        # An uncapped service is the thing this check exists to catch. Treat it
        # as infinite so it can never silently pass the sum.
        return sys.maxsize
    if value[-1].upper() in _SUFFIX:
        return int(float(value[:-1]) * _SUFFIX[value[-1].upper()])
    return int(value)


def systemd_show(unit: str, prop: str) -> str:
    r = subprocess.run(
        ["systemctl", "show", unit, "-p", prop, "--value"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"systemctl show {unit} {prop} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def cgroup_value(unit: str, filename: str) -> int | None:
    """Read one integer from a unit's cgroup v2 file. None if unreadable.

    None is a legitimate answer -- a unit that is not running has no cgroup --
    and is handled explicitly at every call site rather than defaulted to a
    number, which would make a missing reading look like a passing one.
    """
    p = _CGROUP_ROOT / unit / filename
    try:
        raw = p.read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return sys.maxsize
    try:
        return int(raw)
    except ValueError:
        return None


def warn_fraction_of(spec: dict) -> float:
    """budget.yaml's `headroom_warn_fraction`, read in one place.

    Two call sites need it — the console rows and the censored-reading
    qualifier — and a literal default repeated at each is the same defect this
    file removed when it deleted `observed_mb`: a declared value copied into
    code, free to diverge from the declaration silently.
    """
    return float(spec.get("headroom_warn_fraction", 0.90))


def censored_observation(unit: str, warn_fraction: float) -> str | None:
    """Detect a live reading that is a FLOOR rather than a measurement.

    `memory.peak >= memory.high` means the cgroup has been held at its soft cap
    since it last started. Everything measured in that state is bounded by the
    cap itself, so `memory.current` is the largest value the ceiling permitted --
    not the service's working set.

    This tell has been found BY HAND three times on this box (litellm-proxy,
    metron-api/config-I5216, dashboard/config-I5237) and written into a prose
    `note:` each time instead of a check. Each time, the cap was then raised to
    just above the censored floor and the service re-pinned within a day,
    because the floor was never the working set. config-I5237 raised
    dashboard.service 210M -> 260M and it was throttling again the next day.

    It survived the removal of `observed_mb` -- and matters MORE without it --
    because the steady-state sum is now read straight from these cgroups. A
    censored unit makes that sum read safer than it is, so the bound is reported
    as a floor rather than as a pass. That is the difference between an unproven
    invariant and a satisfied one, and this is the only thing that can tell them
    apart.

    `peak >= high` IS NOT SUFFICIENT ON ITS OWN (2026-08-03). `memory.peak` is
    a high-water mark that never decays short of a restart, so a service that
    grazed its soft cap ONCE reads CENSORED for the rest of its uptime even if
    it has sat comfortably below the cap ever since. Measured: metron-api.service
    read `peak 280 == high 280` after three days up, so it was reported censored
    on every tick -- but with its cap temporarily raised 280M -> 480M it did not
    move at all across thirteen minutes (current flat at 214 MiB, peak flat at
    280, zero new MemoryHigh events, `some avg10=0.00`). Its working set is
    ~214 MiB. Nothing was suppressed; the reading was never a floor.

    That false positive is not free even at hygiene severity: it names a service
    whose only correct action is "do nothing", and its own remedy text says
    "raise the cap" -- against a box with 1.26x of a 1.27x overcommit bound
    already spent. A finding that recommends spending scarce headroom on a
    non-problem is worse than silence.

    So the verdict now requires the pin to be CURRENT, not merely historical:
    the service must also be sitting at or above `headroom_warn_fraction` of its
    soft cap right now. Checked against every real instance this check exists
    for -- vires 115/112 (103%), dashboard 335/340 (98.5%), metron-api at the
    time of config-I5216 384/385 (99.7%) -- all still fire. metron-api today,
    at 214/280 (76%), does not.
    """
    peak = cgroup_value(unit, "memory.peak")
    high = cgroup_value(unit, "memory.high")
    current = cgroup_value(unit, "memory.current")
    if peak is None or high is None or high == sys.maxsize:
        return None
    if peak < high:
        return None
    # Historical touch without a current pin: the high-water mark is real, the
    # censorship is not. Silent rather than reported -- box_health.sh's throttle
    # delta and the memory-pressure check both fire on a service that IS being
    # held down, so this case is covered by signals that key on the present.
    if current is not None and current < warn_fraction * high:
        return None
    return (
        f"{unit}: CENSORED reading -- memory.peak ({peak // 1024**2} MiB) has "
        f"reached memory.high ({high // 1024**2} MiB), so this service has been "
        f"pinned at its soft cap since it last started. Its memory.current is a "
        f"FLOOR, not a working set, and the steady-state sum below understates "
        f"by an unknown amount. Raise the cap clear of the service and let it "
        f"run un-pinned -- do NOT re-cap to just above the pinned number."
    )


#: How close to `memory.high` a peak may come before `approaching_the_cap`
#: reports it. Below the pin, deliberately: the whole point is to fire while
#: the reading is still uncensored.
APPROACHING_FRACTION = 0.90


def approaching_the_cap(unit: str, warn_fraction: float) -> str | None:
    """A unit about to censor itself, reported while the fix is still free.

    `censored_observation` above fires once `peak >= high`, which is one MiB
    too late to be cheap. After a unit pins, its `memory.current` is a floor
    and its true demand is unknown, so the raise that buys the reading back has
    to be sized at some multiple of a number nobody trusts — and that multiple
    is guesswork, which is how `nousergon-console` was raised 80M -> 160M at
    "~2x the censored floor" and re-pinned inside a day.

    Before it pins, none of that applies. The reading is real, the raise can be
    sized to it, and it costs NOTHING against the box's overcommit bound
    because only `memory_high` has to move — `memory_max`, which is what
    `sum(memory_max) <= ceiling * overcommit` actually bounds, stays put. The
    same fix goes from free to expensive at the moment this check would have
    fired.

    Measured 2026-08-12: `crucible-dash-api.service` at peak 244 MiB against a
    245M soft cap — 99.6%, and nothing reported it. Both of the box's censored
    units that day (`nousergon-console`, `dashboard.service`) had crossed this
    line on the way in, twice each, unobserved.

    Carries the SAME current-pin requirement as `censored_observation`, for the
    same reason: `memory.peak` is a high-water mark that never decays short of
    a restart, so a service that grazed 90% once would otherwise report for the
    rest of its uptime. `metron-api` at 214/280 (76% current) is the standing
    counter-example and stays silent here too.
    """
    peak = cgroup_value(unit, "memory.peak")
    high = cgroup_value(unit, "memory.high")
    current = cgroup_value(unit, "memory.current")
    if peak is None or high is None or high == sys.maxsize:
        return None
    # At or past the cap is `censored_observation`'s finding, not this one —
    # one condition, one voice.
    if peak >= high or peak < APPROACHING_FRACTION * high:
        return None
    if current is not None and current < warn_fraction * high:
        return None
    return (
        f"{unit}: APPROACHING its soft cap -- memory.peak ({peak // 1024**2} "
        f"MiB) is at {100 * peak / high:.0f}% of memory.high "
        f"({high // 1024**2} MiB) and the service is sitting there now. Raise "
        f"memory_high in budget.yaml WHILE THE READING IS STILL UNCENSORED: "
        f"memory_max does not need to move, so it costs nothing against the "
        f"overcommit bound, and the new cap can be sized to a real working set "
        f"instead of to a floor. After it pins, neither is true."
    )


def steady_state_mb(
    units: list[str], warn_fraction: float
) -> tuple[int, list[str], list[str]]:
    """Measure the steady-state total from the cgroups, with its caveats.

    Returns (total_mb, unmeasurable_units, censored_units).

    The two caveat lists are returned rather than folded into the total because
    they change what the total MEANS. A sum missing a unit understates; a sum
    including a censored unit understates by an unknown amount. Reporting
    "1410 MB, within the limit" while either is true would be the same defect
    the declared numbers had -- a bound that reads satisfied because its input
    is wrong in the safe direction.
    """
    total, unmeasurable, censored = 0, [], []
    for unit in units:
        current = cgroup_value(unit, "memory.current")
        if current is None or current == sys.maxsize:
            unmeasurable.append(unit)
            continue
        total += current // 1024**2
        if censored_observation(unit, warn_fraction):
            censored.append(unit)
    return total, unmeasurable, censored


def memory_events_high(unit: str) -> int | None:
    """The cgroup's lifetime MemoryHigh event count, or None if unreadable.

    Separate from cgroup_value() because memory.events is a key/value table,
    not a bare integer. None rather than 0 on failure: a unit whose counter
    cannot be read has not been shown to be quiet, and rendering that as zero
    is absence-of-signal read as health.
    """
    p = _CGROUP_ROOT / unit / "memory.events"
    try:
        raw = p.read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("high "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def throttle_baseline() -> dict[str, int]:
    """Per-unit counters as of the END of box_health.sh's previous run.

    Empty is a legitimate, expected state (first run after a deploy or reboot)
    and every caller treats it as "no comparison available" rather than as a
    zero baseline -- a zero baseline would report the cgroup's LIFETIME total
    as this tick's delta, which is the exact defect box_health.sh's
    classify_throttle_delta was rewritten to remove (config-I5216).
    """
    try:
        raw = _THROTTLE_STATE.read_text()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def headroom_rows(spec: dict) -> list[dict]:
    """One row per unit in budget.yaml, for the console.

    POPULATION IS budget.yaml, NEVER A LIST MAINTAINED HERE. observability-
    policy.md §2.2: "coverage is derived from this registry and never
    hand-listed. A hand-maintained list drifts silently -- the dashboard box's
    watchdog had drifted to 8 of 14 services and omitted nginx, the sole
    ingress for ten vhosts."

    Every unit yields a row, including one that cannot be measured. A unit
    dropped for being unreadable is a unit that renders as nothing, and nothing
    renders as fine.
    """
    warn_fraction = warn_fraction_of(spec)
    baseline = throttle_baseline()
    rows: list[dict] = []

    for svc in spec["services"]:
        unit = svc["unit"]
        current = cgroup_value(unit, "memory.current")
        high = cgroup_value(unit, "memory.high")
        hard = cgroup_value(unit, "memory.max")
        peak = cgroup_value(unit, "memory.peak")
        events = memory_events_high(unit)
        prev = baseline.get(unit)

        # A counter that went BACKWARDS means the cgroup was recreated (the
        # service restarted) between runs. Not throttling, and not an error --
        # the next run re-baselines. Reported as unknown rather than as a
        # negative or a zero.
        delta: int | None = None
        if events is not None and prev is not None and events >= prev:
            delta = events - prev

        row = {
            "unit": unit,
            "current_mb": None if current is None else current // 1024**2,
            "high_mb": None if high in (None, sys.maxsize) else high // 1024**2,
            "max_mb": None if hard in (None, sys.maxsize) else hard // 1024**2,
            "peak_mb": None if peak in (None, sys.maxsize) else peak // 1024**2,
            "throttle_delta": delta,
            "censored": bool(censored_observation(unit, warn_fraction)),
            "state": "ok",
        }

        if current is None:
            # Distinct from a healthy reading AND from a breach: the unit is not
            # running, or its cgroup is gone. Which of those it is belongs to
            # box_health.sh's service check, which owns that question -- this
            # row says only that headroom is unmeasurable here.
            row["state"] = "unmeasurable"
        elif row["high_mb"] is None:
            # No soft limit at all: no reclaim window before the hard cap. The
            # aggregate check already treats this as a breach; the row has to
            # agree rather than render a comfortable-looking percentage.
            row["state"] = "no_soft_cap"
        else:
            used = current / high
            row["used_fraction"] = round(used, 3)
            if used > 1.0:
                row["state"] = "over_soft_cap"
            elif used >= warn_fraction:
                row["state"] = "tight"
            if row["censored"]:
                # Censored outranks tight: the number the percentage is computed
                # from is a FLOOR, so "88% of cap" is not a measurement. This box
                # has been wrong about dashboard.service's working set twice for
                # exactly this reason (202 MiB and 248 MiB, both floors).
                row["state"] = "censored"
            if delta is not None and delta >= THROTTLE_DELTA_MIN and row["state"] == "ok":
                row["state"] = "throttling"

        rows.append(row)

    return rows


def _row_detail(row: dict) -> str:
    """The one line an operator reads on the console row."""
    if row["state"] == "unmeasurable":
        return "no cgroup -- not running, or the unit is gone (see the service check)"
    parts = []
    if row["high_mb"] is None:
        parts.append(f"{row['current_mb']} MiB, NO soft cap (no reclaim window)")
    else:
        parts.append(
            f"{row['current_mb']}/{row['high_mb']} MiB soft "
            f"({row.get('used_fraction', 0) * 100:.0f}%)"
        )
    if row["max_mb"] is not None:
        parts.append(f"max {row['max_mb']} MiB")
    if row["peak_mb"] is not None:
        parts.append(f"peak {row['peak_mb']} MiB")
    if row["throttle_delta"] is None:
        # Not "0". No baseline, or the cgroup was recreated -- neither of which
        # is evidence that nothing throttled.
        parts.append("throttle delta unknown (no baseline yet)")
    else:
        parts.append(f"+{row['throttle_delta']} throttle events this tick")
    if row["censored"]:
        parts.append("CENSORED: peak has reached the soft cap, so current is a FLOOR")
    return ", ".join(parts)


# The row states that are findings, and the envelope status each implies. The
# console's vocabulary is ok/attention/error; this table is the only place the
# mapping lives so a new row state cannot silently inherit "ok".
_STATE_STATUS = {
    "ok": "ok",
    "throttling": "attention",
    "tight": "attention",
    "censored": "attention",
    "unmeasurable": "attention",
    "over_soft_cap": "error",
    "no_soft_cap": "error",
}


def emit_headroom_check(spec: dict, breaches: list[str], hygiene: list[str],
                        *, dry_run: bool = False) -> str | None:
    """Publish the fleet-check envelope the console discovers by S3 prefix.

    Imported lazily and never allowed to raise: --declared runs in CI, where
    nousergon-lib is not installed, and a check must not go red because its
    telemetry did (the emitter itself makes the same guarantee on the S3 side).
    """
    try:
        from nousergon_lib import fleet_check_result as fcr
    except ImportError:
        print("check_memory_budget: nousergon_lib.fleet_check_result "
              "unavailable -- console row NOT published", file=sys.stderr)
        return None

    rows = headroom_rows(spec)
    findings = [{"key": r["unit"], "detail": _row_detail(r)} for r in rows]

    # Timer jobs get rows too, declared as batch rather than silently dropped.
    # Their cgroups exist only while running, so an absent one is expected here
    # and must not read as a finding -- but neither may the unit vanish from the
    # surface (observability-policy.md §8.3 forbids a component disappearing).
    for job in spec.get("timer_jobs") or []:
        findings.append({
            "key": job["unit"],
            "detail": f"batch unit, cap {job['memory_max']} -- no cgroup between "
                      f"runs, so headroom is not evaluable at rest (by design)",
        })

    for b in breaches:
        findings.append({"key": "BREACH", "detail": b})
    for h in hygiene:
        findings.append({"key": "hygiene", "detail": h})

    status = fcr.STATUS_OK
    if breaches:
        status = fcr.STATUS_ERROR
    for r in rows:
        implied = _STATE_STATUS.get(r["state"], fcr.STATUS_ERROR)
        if implied == fcr.STATUS_ERROR:
            status = fcr.STATUS_ERROR
        elif implied == fcr.STATUS_ATTENTION and status == fcr.STATUS_OK:
            status = fcr.STATUS_ATTENTION
    if hygiene and status == fcr.STATUS_OK:
        status = fcr.STATUS_ATTENTION

    # The summary names the TIGHTEST unit, because that is the one that decides
    # whether the next cap raise is coming. A summary that reports an average
    # would have read comfortably on 2026-07-31, when dashboard.service sat at
    # 98.5% of its soft cap and the box had 1688 MiB free.
    measured = [r for r in rows if r.get("used_fraction") is not None]
    throttled = [r for r in rows if (r["throttle_delta"] or 0) >= THROTTLE_DELTA_MIN]
    if measured:
        worst = max(measured, key=lambda r: r["used_fraction"])
        summary = (f"{worst['unit']} at {worst['used_fraction'] * 100:.0f}% of its "
                   f"soft cap ({worst['current_mb']}/{worst['high_mb']} MiB); "
                   f"{len(throttled)} unit(s) throttling this tick")
    else:
        summary = "no unit's memory could be measured -- headroom is unknown, not fine"
    if breaches:
        summary = f"{len(breaches)} budget breach(es); " + summary

    return fcr.emit_result(
        check_id=CHECK_ID, label=CHECK_LABEL, status=status, summary=summary,
        cadence_minutes=CHECK_CADENCE_MINUTES, findings=findings, dry_run=dry_run,
    )


# Memory drop-ins that legitimately exist without a budget.yaml entry.
# BY NAME WITH A REASON, for the same argument the manifest exclusions carry: a
# bare "ignore anything unexpected" cannot say what it is ignoring.
#
# EMPTY as of 2026-07-29. Its only entry was morning-signal.service, suppressed
# as "timer-driven, out of this budget's scope" -- which meant its 900M cap, the
# largest single claim on the box's headroom, was known to this file only as a
# name on an ignore list. It is now DECLARED in budget.yaml's `timer_jobs:`
# instead, so it is drift-checked and counted like everything else. Prefer
# declaring to suppressing: an entry here says "we looked and it is fine", and
# nothing re-examines that claim.
DROPIN_ALLOW: set[str] = set()

_DROPIN_ROOT = pathlib.Path("/etc/systemd/system")

# `systemctl set-property --runtime <unit> MemoryMax=...` writes here, e.g.
# /run/systemd/system.control/metron-api.service.d/50-MemoryMax.conf. systemd
# merges drop-ins from BOTH /etc/systemd/system and /run/systemd/system.control
# (plus /usr/lib), and /run wins ties by mount precedence regardless of
# filename -- so a --runtime override outranks the generated
# /etc/.../99-resource-limits.conf no matter what it is named.
#
# Root-owned and cleared on reboot: its absence is the normal case, not a
# finding, and a test must not assume it persists. Module constant, same
# pattern as _DROPIN_ROOT and _CGROUP_ROOT, so it is testable off-box.
#
# alpha-engine-config-I6277: measured live on i-09b539c844515d549,
# 2026-08-03 17:11-17:40 UTC -- metron-api.service ran with an EFFECTIVE
# MemoryHigh/MemoryMax of 480M/560M while budget.yaml declared 280M/350M and
# the /etc drop-in agreed with budget.yaml. `daemon-reload` does not touch
# /run, so re-running install-resource-limits.sh after such a drift silently
# changes nothing on the running unit.
_RUNTIME_DROPIN_ROOT = pathlib.Path("/run/systemd/system.control")


def runtime_dropin_overrides(unit: str) -> list[str]:
    """Memory-setting drop-ins for `unit` under systemd's --runtime tier.

    Returns the conf file paths (as strings) so the caller can name them
    verbatim in a finding. Empty list is the normal case -- most units have no
    live override -- and is returned rather than raised for a missing
    _RUNTIME_DROPIN_ROOT/<unit>.d directory, which is expected whenever no
    --runtime property has ever been set or the box has rebooted since.
    """
    unit_dir = _RUNTIME_DROPIN_ROOT / f"{unit}.d"
    found: list[str] = []
    if not unit_dir.is_dir():
        return found
    for conf in sorted(unit_dir.glob("*.conf")):
        try:
            body = conf.read_text()
        except OSError:
            continue
        if "MemoryMax=" in body or "MemoryHigh=" in body:
            found.append(str(conf))
    return found


def uncensor_deadline(svc: dict) -> "_dt.datetime | None":
    """Parse a service's optional `uncensor_until: <ISO8601>` key.

    A malformed value is treated as ABSENT, not raised: a typo in a
    measurement-window field must fail toward the loud outcome (an ordinary
    breach) rather than crash the whole check.
    """
    raw = svc.get("uncensor_until")
    if not raw:
        return None
    try:
        deadline = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=_dt.timezone.utc)
    return deadline


def uncensor_active(svc: dict, now: "_dt.datetime | None" = None) -> bool:
    """True while `svc` declares an uncensor_until that has not yet passed.

    This is the measurement window alpha-engine-config-I6263's
    `systemctl set-property --runtime` procedure needs: the live cap
    genuinely must exceed the declared one for a while to un-censor a
    reading. Absence of the key, or a deadline already in the past, is NOT
    active -- an abandoned measurement gets LOUDER (a breach), never quieter.
    """
    deadline = uncensor_deadline(svc)
    if deadline is None:
        return False
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now < deadline


def orphan_dropins(budget_units: set[str]) -> list[str]:
    """Find installed memory drop-ins that no budget entry owns.

    install-resource-limits.sh only ever visits units listed in budget.yaml, so
    a drop-in belonging to a unit that is NOT listed is never inspected, never
    cleaned up, and never checked -- it is invisible to the whole mechanism
    while still being live systemd config.

    Found on 2026-07-28: alpha-engine-dashboard.service.d/memory-limit.conf,
    setting MemoryMax=300M/MemoryHigh=250M for a unit that does not exist at all
    (`systemctl is-enabled` -> "No such file or directory"). Harmless in that
    instance only because the unit is absent; the same gap would just as happily
    hide a stale cap on a unit that IS running, which is precisely the
    "hand-edited drop-in" class this script's docstring claims to catch.
    """
    found = []
    for conf in sorted(_DROPIN_ROOT.glob("*.service.d/*.conf")):
        try:
            body = conf.read_text()
        except OSError:
            continue
        if "MemoryMax=" not in body and "MemoryHigh=" not in body:
            continue
        unit = conf.parent.name[: -len(".d")]
        if unit in budget_units or unit in DROPIN_ALLOW:
            continue
        unit_exists = (_DROPIN_ROOT / unit).exists() or any(
            pathlib.Path(d, unit).exists()
            for d in ("/usr/lib/systemd/system", "/lib/systemd/system")
        )
        found.append(
            f"ORPHAN drop-in {conf}: sets memory limits for {unit}, which has no "
            f"budget.yaml entry"
            + ("" if unit_exists else " and no unit file on disk")
            + ". Either add it to the budget or delete the drop-in -- an "
            "unowned limit is live config that nothing reviews."
        )
    return found


def ram_mb_from_proc() -> int:
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemTotal not found in /proc/meminfo")


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--declared", action="store_true",
                      help="check budget.yaml against itself (CI mode)")
    mode.add_argument("--installed", action="store_true",
                      help="check systemd's loaded values and assert no drift (on-box mode)")
    ap.add_argument("--quiet", action="store_true", help="only print on failure")
    ap.add_argument("--emit-check", action="store_true",
                    help="publish the per-unit headroom fleet-check envelope to "
                         "the console surface (implies --installed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --emit-check, build the envelope but write nothing")
    args = ap.parse_args()
    # --emit-check renders live cgroup facts, which exist only on the box. Off-box
    # there is nothing to render, and publishing a declared-mode envelope would put
    # numbers on the console that no cgroup ever produced.
    installed = (args.installed or args.emit_check) and not args.declared

    spec = yaml.safe_load(BUDGET.read_text())
    reserve = float(spec["reserve_fraction"])

    # Two lists, deliberately not one. `breaches` are invariant violations and
    # page; `hygiene` are findings about the QUALITY OF THE MEASUREMENT and do
    # not. Merging them is what made a stale number page like a full box.
    breaches: list[str] = []
    hygiene: list[str] = []

    # In --installed mode the real RAM is authoritative: it catches a resize
    # that nobody reflected back into budget.yaml, in either direction.
    if installed:
        ram_mb = ram_mb_from_proc()
        declared_ram = int(spec["ram_mb"])
        if abs(ram_mb - declared_ram) > declared_ram * 0.05:
            print(f"BREACH: budget.yaml declares ram_mb={declared_ram} but the box "
                  f"has {ram_mb} MB. Re-budget after an instance resize.",
                  file=sys.stderr)
            return 1
    else:
        ram_mb = int(spec["ram_mb"])

    ceiling_mb = int(ram_mb * (1 - reserve))

    total_bytes = 0
    # Credited sum: identical to total_bytes except a service currently inside
    # its declared uncensor_until window has its contribution capped at its
    # DECLARED memory_max rather than its inflated live one. This is what lets
    # a deliberate, time-boxed `set-property --runtime` measurement avoid also
    # tripping the aggregate overcommit bound (Bound 1 below) for its
    # duration, without silently excluding the unit from the sum forever --
    # once the window passes the two sums converge again and any real breach
    # reappears automatically.
    total_bytes_credited = 0
    rows = []

    for svc in spec["services"]:
        unit = svc["unit"]
        want = parse_bytes(svc["memory_max"])

        if installed:
            try:
                have = parse_bytes(systemd_show(unit, "MemoryMax"))
                have_high = parse_bytes(systemd_show(unit, "MemoryHigh"))
            except RuntimeError as e:
                breaches.append(f"{unit}: {e}")
                continue
            if have != want:
                drift_msg = (
                    f"{unit}: MemoryMax drift -- budget declares "
                    f"{svc['memory_max']} but systemd has "
                    f"{'infinity' if have == sys.maxsize else str(have // 1024**2) + 'M'}"
                )
                # alpha-engine-config-I6277: name the mechanism, not just the
                # effect. Attached to this same drift line rather than emitted
                # separately, per the issue's deliverable -- a reader must not
                # have to correlate two unrelated-looking findings by hand.
                overrides = runtime_dropin_overrides(unit)
                if overrides:
                    drift_msg += (
                        f". LIVE OVERRIDE at {', '.join(overrides)} from "
                        f"`systemctl set-property --runtime` -- this "
                        f"OUTRANKS the generated /etc drop-in. "
                        f"`daemon-reload` does NOT clear "
                        f"/run/systemd/system.control, so re-running "
                        f"install-resource-limits.sh will NOT fix this. "
                        f"Revert with: systemctl revert {unit} -- then "
                        f"IMMEDIATELY re-run install-resource-limits.sh: "
                        f"revert deletes the persistent /etc drop-in too, "
                        f"leaving the unit UNCAPPED until the installer "
                        f"rewrites it (measured on nginx, 2026-08-09)"
                    )
                deadline = uncensor_deadline(svc)
                if deadline is not None and uncensor_active(svc):
                    hygiene.append(
                        drift_msg + f". Inside its declared uncensor_until "
                        f"window (deadline {deadline.isoformat()}) -- "
                        f"reported as hygiene, not a page, until then. "
                        f"Resolve (revert or extend) before the deadline: an "
                        f"abandoned measurement pages again automatically "
                        f"once it passes."
                    )
                elif deadline is not None:
                    breaches.append(
                        drift_msg + f". uncensor_until "
                        f"({deadline.isoformat()}) has PASSED -- this "
                        f"measurement window is abandoned and pages like any "
                        f"other drift."
                    )
                else:
                    breaches.append(drift_msg)
            if have_high == sys.maxsize:
                breaches.append(
                    f"{unit}: MemoryHigh is unset/infinity -- no reclaim window "
                    f"before the hard cap"
                )
            effective = have
        else:
            effective = want

        total_bytes += effective
        total_bytes_credited += (
            min(effective, want) if installed and uncensor_active(svc) else effective
        )
        rows.append((unit, effective))

    timer_jobs = spec.get("timer_jobs") or []

    if installed:
        hygiene.extend(orphan_dropins(
            {s["unit"] for s in spec["services"]} | {t["unit"] for t in timer_jobs}
        ))
        # Timer jobs are drift-checked exactly like services -- a declared cap
        # that was never installed is the same defect either way. They are only
        # excluded from the SUM below, not from verification.
        for job in timer_jobs:
            unit = job["unit"]
            try:
                have = parse_bytes(systemd_show(unit, "MemoryMax"))
                have_high = parse_bytes(systemd_show(unit, "MemoryHigh"))
            except RuntimeError as e:
                breaches.append(f"{unit}: {e}")
                continue
            if have != parse_bytes(job["memory_max"]):
                breaches.append(
                    f"{unit} (timer job): MemoryMax drift -- budget declares "
                    f"{job['memory_max']} but systemd has "
                    f"{'infinity' if have == sys.maxsize else str(have // 1024**2) + 'M'}"
                )
            if have_high != parse_bytes(job["memory_high"]):
                breaches.append(
                    f"{unit} (timer job): MemoryHigh drift -- budget declares "
                    f"{job['memory_high']} but systemd has "
                    f"{'infinity' if have_high == sys.maxsize else str(have_high // 1024**2) + 'M'}"
                )

    total_mb = total_bytes // 1024**2 if total_bytes < sys.maxsize else -1
    total_mb_credited = (
        total_bytes_credited // 1024**2 if total_bytes_credited < sys.maxsize else -1
    )

    # Bound 1: bounded overcommit. Caps are sized for STARTUP PEAKS, which do
    # not coincide across services, so the sum is allowed to exceed the ceiling
    # by a declared ratio -- but only by a declared one. An unbounded sum is the
    # 2.4x accident that cascaded the box on 2026-07-27.
    max_ratio = float(spec["max_overcommit_ratio"])
    allowed_mb = int(ceiling_mb * max_ratio)
    ratio = (total_mb / ceiling_mb) if ceiling_mb and total_mb > 0 else float("inf")
    # `over` is evaluated against the CREDITED sum (alpha-engine-config-I6277):
    # a service's excess above its declared cap does not count against this
    # bound while its uncensor_until window is active. total_mb itself (the
    # real, uncredited sum) is still what gets printed and published -- only
    # the breach/hygiene DECISION is affected.
    over = total_mb_credited > allowed_mb or total_mb_credited < 0
    if not over and (total_mb > allowed_mb or total_mb < 0):
        hygiene.append(
            f"aggregate MemoryMax {total_mb} MB is {ratio:.2f}x the "
            f"{ceiling_mb} MB ceiling (max {max_ratio:.2f}x) -- OVER ONLY "
            f"because of unit(s) inside an active uncensor_until measurement "
            f"window; credited back to their declared caps it is "
            f"{total_mb_credited} MB, within bound. Becomes a hard breach "
            f"automatically once any of those windows pass without being "
            f"resolved."
        )

    # Bound 2: steady-state safety. This is the one that governs normal
    # operation -- what the services ACTUALLY use must leave real headroom,
    # independent of how generous the caps are. MEASURED from the cgroups, never
    # declared; see the module docstring for why the declared version had to go.
    #
    # Off-box there is nothing to measure, so this bound is SKIPPED and says so.
    # A silent skip here would be the worse of the two options by far: a CI run
    # printing nothing about the bound is indistinguishable from one that
    # checked it and was satisfied.
    max_ss = float(spec["max_steady_state_fraction"])
    ss_allowed = int(ram_mb * max_ss)
    warn_fraction = warn_fraction_of(spec)
    ss_over = False
    ss_mb = 0
    unmeasurable: list[str] = []
    censored: list[str] = []
    if installed:
        ss_mb, unmeasurable, censored = steady_state_mb(
            [s["unit"] for s in spec["services"]], warn_fraction
        )
        ss_over = ss_mb > ss_allowed
        for unit in censored:
            hygiene.append(
                censored_observation(unit, warn_fraction) or f"{unit}: censored"
            )
        # Reported after the censored units and never for the same unit — the
        # two conditions are mutually exclusive by construction (`peak >= high`
        # versus `peak < high`), so a unit appears in at most one list.
        for svc in spec["services"]:
            approaching = approaching_the_cap(svc["unit"], warn_fraction)
            if approaching:
                hygiene.append(approaching)
        if unmeasurable:
            hygiene.append(
                "steady state measured over "
                f"{len(rows) - len(unmeasurable)} of {len(rows)} units -- no "
                f"cgroup for: {', '.join(unmeasurable)}. The sum below "
                f"understates by whatever those are using. (A unit that is not "
                f"running is reported by box_health.sh's service check, not "
                f"here.)"
            )

    # Bound 3: batch headroom (policy §4.3 + §4's batch-job rule). A timer job
    # allocates ON TOP of whatever the long-running services are already holding,
    # so the bound is not "does it fit in RAM" but "does it fit in what is LEFT".
    #
    # Summed rather than maxed: the worst case is every timer firing at once, and
    # the cheap direction to be wrong in is the one where a batch peak cannot
    # evict a user-facing service. Conservative for disjoint schedules, and
    # deliberately so.
    #
    # Installed mode only -- it is measured against the live steady state, and a
    # declared substitute for that is what this file removed on 2026-07-29.
    tj_mb = sum(parse_bytes(t["memory_max"]) for t in timer_jobs) // 1024**2 if timer_jobs else 0
    tj_headroom_mb = ram_mb - ss_mb if installed else 0
    tj_over = bool(installed and timer_jobs and tj_mb > tj_headroom_mb)

    if not args.quiet or over or ss_over or tj_over or breaches or hygiene:
        label = "installed" if installed else "declared"
        print(f"memory budget ({label}): RAM {ram_mb} MB, reserve {reserve:.0%}, "
              f"ceiling {ceiling_mb} MB, max overcommit {max_ratio:.2f}x "
              f"(= {allowed_mb} MB)")
        for unit, b in sorted(rows, key=lambda r: -r[1]):
            print(f"  {unit:<28} {b // 1024**2:>5} MB")
        print(f"  {'TOTAL (caps)':<28} {total_mb:>5} MB  "
              f"{ratio:.2f}x ceiling "
              f"({'OVER' if over else 'within declared overcommit'})")
        if installed:
            # "FLOOR" rather than "ok" whenever the reading is impaired: the
            # bound is unproven, not satisfied, and the word has to say so.
            impaired = bool(censored or unmeasurable)
            verdict = "OVER" if ss_over else ("FLOOR, unproven" if impaired else "ok")
            print(f"  {'TOTAL (steady state)':<28} {ss_mb:>5} MB  "
                  f"{ss_mb / ram_mb:.0%} of RAM "
                  f"({verdict}, limit {max_ss:.0%})")
        else:
            print(f"  {'TOTAL (steady state)':<28} {'--':>5}     "
                  f"not evaluable off-box (measured from cgroups by "
                  f"box_health.sh --installed; limit {max_ss:.0%})")
        if timer_jobs:
            for t in sorted(timer_jobs, key=lambda t: -parse_bytes(t["memory_max"])):
                print(f"  [timer] {t['unit']:<20} {parse_bytes(t['memory_max']) // 1024**2:>5} MB")
            if installed:
                print(f"  {'TOTAL (timer job caps)':<28} {tj_mb:>5} MB  "
                      f"vs {tj_headroom_mb} MB headroom "
                      f"({'OVER' if tj_over else 'ok'})")
            else:
                print(f"  {'TOTAL (timer job caps)':<28} {tj_mb:>5} MB  "
                      f"headroom not evaluable off-box")

    for p in breaches:
        print(f"BREACH: {p}", file=sys.stderr)
    for p in hygiene:
        print(f"HYGIENE: {p}", file=sys.stderr)

    if over:
        print(f"BREACH: aggregate MemoryMax {total_mb} MB is {ratio:.2f}x the "
              f"{ceiling_mb} MB ceiling, above the declared "
              f"max_overcommit_ratio {max_ratio:.2f}x. Either lower a cap, raise "
              f"the ratio WITH a written rationale, or move a service off this "
              f"box (policy T1-1 / decision framework section 4).", file=sys.stderr)
    if ss_over:
        print(f"BREACH: steady-state total {ss_mb} MB is {ss_mb / ram_mb:.0%} of "
              f"RAM, above the {max_ss:.0%} limit. This is the bound that governs "
              f"normal operation -- the box is genuinely too small for what it "
              f"runs (policy T1-7 / exit trigger E3).", file=sys.stderr)

    if tj_over:
        print(f"BREACH: timer-job caps total {tj_mb} MB against only "
              f"{tj_headroom_mb} MB of headroom left by the running services. A "
              f"batch peak that cannot fit in the headroom evicts a user-facing "
              f"service instead. Either lower a cap against a fresh measurement "
              f"or move the job to a spot instance or Lambda (policy section 4, "
              f"batch-job rule).", file=sys.stderr)

    # Published AFTER every finding is collected and BEFORE the exit code is
    # chosen, so the console row carries the same verdict the exit code does.
    # Deliberately unconditional on that verdict: a check that publishes only
    # when it has something to say is indistinguishable, from the console, from
    # a check that has stopped running (observability-policy.md §8.3 --
    # UNREPORTED must never collapse into HEALTHY).
    if args.emit_check:
        aggregate: list[str] = list(breaches)
        if over:
            aggregate.append(
                f"aggregate MemoryMax {total_mb} MB is {ratio:.2f}x the "
                f"{ceiling_mb} MB ceiling (max {max_ratio:.2f}x)")
        if ss_over:
            aggregate.append(
                f"steady-state total {ss_mb} MB is {ss_mb / ram_mb:.0%} of RAM "
                f"(limit {max_ss:.0%})")
        if tj_over:
            aggregate.append(
                f"timer-job caps total {tj_mb} MB against {tj_headroom_mb} MB "
                f"of headroom")
        emit_headroom_check(spec, aggregate, hygiene, dry_run=args.dry_run)

    if over or ss_over or tj_over or breaches:
        return 1
    return 2 if hygiene else 0


if __name__ == "__main__":
    sys.exit(main())
