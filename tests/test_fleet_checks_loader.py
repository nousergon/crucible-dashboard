"""Tests for the fleet check-result surface (config-I5548 / I5507).

The load-bearing behaviour is the STALENESS OVERRIDE: the last thing a dying
check writes is usually `"status": "ok"`, so a surface that trusts the reported
status renders a check that stopped running two weeks ago as green. That is the
same absence-read-as-benign defect that let four scheduled workflows sit dark on
2026-07-29 — this page exists because of it, so it is tested first.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders import fleet_checks_loader as fcl  # noqa: E402

NOW = datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc)


def envelope(**over):
    base = {
        "schema_version": 1,
        "check_id": "iam_grant_usage",
        "label": "IAM grant usage",
        "ran_at": (NOW - timedelta(hours=1)).isoformat(),
        "status": "ok",
        "summary": "4 of 79 grants never used",
        "cadence_minutes": 10080,
        "findings": [],
    }
    base.update(over)
    return base


# --- staleness override ----------------------------------------------------

def test_a_check_that_stopped_running_is_not_green():
    """Reported ok, but the artifact is 3 weeks old on a weekly cadence."""
    e = envelope(ran_at=(NOW - timedelta(days=21)).isoformat())
    r = fcl.interpret(e, check_id="x", now=NOW)
    assert r.status == fcl.STATUS_STALE
    assert not r.is_healthy
    assert "last reported: ok" in r.summary, "the stale row must still say what it claimed"


def test_within_cadence_keeps_the_reported_status():
    r = fcl.interpret(envelope(), check_id="x", now=NOW)
    assert r.status == fcl.STATUS_OK


def test_one_missed_run_is_tolerated_before_stale():
    """A late run must not flap the dot; 2.5 cadences is one full miss + slack."""
    e = envelope(ran_at=(NOW - timedelta(days=9)).isoformat())  # 1.3 cadences
    assert fcl.interpret(e, check_id="x", now=NOW).status == fcl.STATUS_OK


def test_missing_cadence_defaults_rather_than_disabling_staleness():
    """An envelope without cadence_minutes must not become never-stale."""
    e = envelope(cadence_minutes=None, ran_at=(NOW - timedelta(days=5)).isoformat())
    r = fcl.interpret(e, check_id="x", now=NOW)
    assert r.cadence_minutes == fcl.DEFAULT_CADENCE_MINUTES
    assert r.status == fcl.STATUS_STALE


# --- unreadable inputs -----------------------------------------------------

def test_missing_artifact_is_unreadable_not_ok():
    r = fcl.interpret(None, check_id="ghost", now=NOW)
    assert r.status == fcl.STATUS_UNREADABLE
    assert not r.is_healthy


def test_unparseable_ran_at_is_unreadable():
    r = fcl.interpret(envelope(ran_at="not-a-date"), check_id="x", now=NOW)
    assert r.status == fcl.STATUS_UNREADABLE


def test_naive_timestamp_is_treated_as_utc():
    """Producers occasionally omit the offset; a naive parse must not throw or
    silently shift the age by the local offset."""
    e = envelope(ran_at="2026-07-29T19:00:00")
    assert fcl.interpret(e, check_id="x", now=NOW).status == fcl.STATUS_OK


def test_zulu_suffix_parses():
    e = envelope(ran_at="2026-07-29T19:00:00Z")
    assert fcl.interpret(e, check_id="x", now=NOW).status == fcl.STATUS_OK


# --- pass-through ----------------------------------------------------------

def test_attention_and_findings_survive():
    e = envelope(status="attention", summary="1 grant newly used",
                 findings=[{"key": "role:states", "detail": "newly used"}])
    r = fcl.interpret(e, check_id="x", now=NOW)
    assert r.status == fcl.STATUS_ATTENTION
    assert len(r.findings) == 1


def test_label_falls_back_to_check_id():
    e = envelope()
    del e["label"]
    assert fcl.interpret(e, check_id="some_check", now=NOW).label == "some_check"


# --- resolver --------------------------------------------------------------

def _res(status, label="c", summary="s", ran_at=NOW):
    return fcl.CheckResult(label, label, status, summary, ran_at, 1440, None, ())


def test_resolver_reports_gray_when_the_probe_is_unavailable():
    """Empty must never render green — an unreadable check surface looks
    identical to a healthy one otherwise."""
    import fleet_status as fs
    inp = fs.FleetInputs(now=NOW, is_trading_day=True, check_envelopes=())
    assert fs.resolve_fleet_checks(inp).dot == fs.GRAY


@pytest.mark.parametrize("status,expected_attr", [
    (fcl.STATUS_ERROR, "RED"),
    (fcl.STATUS_STALE, "RED"),
    (fcl.STATUS_UNREADABLE, "RED"),
    (fcl.STATUS_ATTENTION, "YELLOW"),
    (fcl.STATUS_OK, "GREEN"),
])
def test_resolver_dot_follows_the_worst_check(status, expected_attr):
    import fleet_status as fs
    inp = fs.FleetInputs(now=NOW, is_trading_day=True,
                         check_envelopes=(_res(fcl.STATUS_OK), _res(status)))
    assert fs.resolve_fleet_checks(inp).dot == getattr(fs, expected_attr)


def test_resolver_row_carries_one_detail_row_per_check():
    import fleet_status as fs
    inp = fs.FleetInputs(now=NOW, is_trading_day=True,
                         check_envelopes=(_res(fcl.STATUS_OK, "a"),
                                          _res(fcl.STATUS_OK, "b")))
    assert len(fs.resolve_fleet_checks(inp).detail) == 2


def test_resolver_is_registered_in_the_fleet_rollup():
    """A resolver nobody calls is the defect this whole surface is about."""
    import inspect

    import fleet_status as fs
    assert "resolve_fleet_checks(inp)" in inspect.getsource(fs.resolve_fleet)


def test_deep_link_slug_resolves_on_the_page():
    """§118 rule 4 — the row must link to its evidence, and the chokepoint
    test only checks slugs that exist in the page's URL map."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "views", "48_Fleet_Status.py")).read()
    assert '"fleet-checks"' in src
