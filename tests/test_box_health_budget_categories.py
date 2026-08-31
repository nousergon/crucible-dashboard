"""The memory-budget alert names what was found, not where to look for it.

Before this, box_health.sh published two strings verbatim:

    memory budget: BREACH (detail in journal)
    notice: memory budget observation hygiene (detail in journal)

Both instruct the reader to go and read a journal that the emitter is running
on and has already read. Measured on the live krepis dedup markers (read
2026-08-31): `boxhealth-info-notice:_memory_budget_observation_hygiene_(detail
_in_journal)` carried 13 publishes and the `warning` breach variant 7 -- twenty
deliveries of a message whose entire content was an assignment.

The replacement is a CATEGORY SET rather than the finding text, because
box_health.sh's confirm-on-retry intersection matches problem lines
byte-for-byte and the critical tier derives its dedup key from the problem set.
The finding strings carry live byte counts; a category does not.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "infrastructure" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cmb = _load("check_memory_budget")
BOX_HEALTH = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()


@pytest.mark.parametrize(
    "line,expected",
    [
        ("litellm-proxy.service: MemoryMax drift -- budget declares 512M but "
         "systemd has 256M. LIVE OVERRIDE at /run/systemd/system.control",
         "runtime-cap-override"),
        ("nginx.service: MemoryMax drift -- budget declares 512M but systemd "
         "has infinity", "cap-drift"),
        ("ORPHAN drop-in /etc/systemd/system/x.service.d/50-mem.conf: sets "
         "memory limits for x.service, which has no budget.yaml entry",
         "orphan-dropin"),
        ("console.service: CENSORED reading -- memory.peak (280 MiB) has "
         "reached memory.high (280 MiB)", "censored-observation"),
        ("console.service: APPROACHING its soft cap -- memory.peak (250 MiB)",
         "approaching-cap"),
        ("dashboard.service: OVER-PROVISIONED -- memory.max (2048 MiB) is 8.1x",
         "over-provisioned-cap"),
        ("peak marks not writable (/var/lib/x): [Errno 13]",
         "peak-marks-unwritable"),
        ("steady-state bound measured over 3 of 5 units -- memory.stat "
         "unreadable for: a.service", "working-set-unmeasurable"),
        ("steady state measured over 3 of 5 units -- no cgroup for: b.service",
         "unit-unmeasurable"),
        ("BREACH: budget.yaml declares ram_mb=7900 but the box has 3800 MB.",
         "ram-declaration-drift"),
        ("aggregate MemoryMax 9000 MB is 1.30x the 6900 MB ceiling",
         "aggregate-overcommit"),
        ("steady-state working set 4000 MB is 51% of RAM",
         "steady-state-overcommit"),
        ("timer-job caps total 900 MB against 400 MB of headroom",
         "timer-job-headroom"),
    ],
)
def test_every_vocabulary_row_is_reachable(line, expected):
    assert cmb.finding_category(line) == expected


def test_the_runtime_override_outranks_plain_cap_drift():
    """The override message CONTAINS the drift message, and the override is the
    actionable half -- `systemctl revert`, not another installer run. A matcher
    ordered the other way would answer `cap-drift` for both and send the
    operator to a remedy that provably does not work (measured on nginx,
    2026-08-09)."""
    both = ("nginx.service: MemoryMax drift -- budget declares 512M but "
            "systemd has 256M. LIVE OVERRIDE at /run/systemd/system.control")
    assert cmb.finding_category(both) == "runtime-cap-override"


def test_an_unrecognised_finding_is_unattributed_not_a_default():
    """The load-bearing negative. A finding added to check_memory_budget.py
    without a row here must read as UNKNOWN, never inherit whichever label
    happened to be last -- otherwise a new check silently mislabels its own
    findings and nobody can tell."""
    assert cmb.finding_category(
        "some future finding nobody has written a category for"
    ) == "unattributed"


def test_the_summary_is_a_sorted_deduplicated_set():
    """A function of the SET, not of evaluation order: a reordering that
    changed no condition must not produce a different alert string, or the
    dedup key moves for nothing."""
    lines = [
        "a.service: CENSORED reading -- x",
        "b.service: CENSORED reading -- y",
        "c.service: APPROACHING its soft cap -- z",
    ]
    assert cmb.category_summary(lines) == "approaching-cap,censored-observation"
    assert cmb.category_summary(list(reversed(lines))) == \
        cmb.category_summary(lines)


def test_the_summary_ignores_blank_lines():
    assert cmb.category_summary(["", "   ", "x: CENSORED reading"]) == \
        "censored-observation"


class TestTheAlertText:
    def test_neither_budget_line_still_points_at_the_journal(self):
        for want in ("memory budget: BREACH",
                     "notice: memory budget observation hygiene"):
            m = [ln for ln in BOX_HEALTH.splitlines()
                 if f'echo "{want}' in ln]
            assert m, f"the {want!r} finding is no longer emitted at all"
            assert "detail in journal" not in m[0], (
                f"{want!r} still tells the reader to go and read a journal the "
                "emitter has already read: " + m[0].strip()
            )

    def test_the_alert_interpolates_the_category_set(self):
        assert 'echo "memory budget: BREACH ($budget_cats)"' in BOX_HEALTH
        assert ('echo "notice: memory budget observation hygiene '
                '($budget_cats)"') in BOX_HEALTH

    def test_a_missing_categories_line_is_reported_not_papered_over(self):
        """An emitter that found something and said nothing about WHAT is a
        defect in that emitter. Falling back to prose would hide it."""
        assert 'budget_cats="unreported"' in BOX_HEALTH

    def test_the_producer_emits_the_line_box_health_parses(self):
        producer = (REPO_ROOT / "infrastructure" / "check_memory_budget.py").read_text()
        assert 'box-health-categories: ' in producer
        # The consumer's sed expression and the producer's prefix are one
        # contract; a rename on either side is a silent `unreported` forever.
        m = re.search(r"sed -n 's/\^([^/]+): //p'", BOX_HEALTH)
        assert m, "box_health.sh no longer parses a categories prefix"
        assert m.group(1) == "box-health-categories"
