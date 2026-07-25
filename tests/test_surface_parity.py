"""Surface-parity chokepoint: every console RED must have paged, every page
must be console-visible (alpha-engine-config#3208, ARCHITECTURE.md §118 rule 5).

Reconciles the Fleet Status console plane (``fleet_status.resolve_artifact_freshness``)
against the freshness-monitor's independently-derived alert decisions
(``nousergon-data/infrastructure/lambdas/freshness-monitor/index.py`` —
specifically ``_maybe_alert``'s severity logic, including warning escalation
at ``WARNING_ESCALATION_RUNS=3`` consecutive confirmed misses).

The two planes compose:
  - **Freshness monitor** (``nousergon-data``) decides what PAGES: ``severity=critical``
    + ``state ∈ {missing, stale, probe_failed}`` past SLA grace. A ``severity=warning``
    row escalated to critical after ``WARNING_ESCALATION_RUNS`` consecutive confirmed
    misses ALSO pages (config-I3086), but ``check_results.json`` persists the original
    ``severity: "warning"`` — creating the exact divergence this chokepoint catches.
  - **Fleet Status** (``crucible-dashboard``) decides what is RED for the
    ``artifact_freshness`` component: same ``check_results.json``, but only rows where
    ``severity == "critical"`` AND ``state ∈ {missing, stale, probe_failed}``.
    Escalated warnings are NOT seen as critical — hence the gap.

This test covers the **page → console RED** direction: if the freshness-monitor would
have paged for a set of artifact-level conditions, the Fleet Status ``artifact_freshness``
dot must carry a ``RED`` (or at minimum ``YELLOW``) severity. The reverse direction
(console RED → page) is out of scope for this first landing and filed as a fast-follow
per the issue's own closing conditions.

chokepoint-identity: surface-parity-v1  (config#3208)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fleet_status import (  # noqa: E402
    RED,
    YELLOW,
    FleetInputs,
    resolve_artifact_freshness,
    resolve_fleet,
)

# Reference clock (trading day mid-session, same as test_fleet_status.py).
TRADING_MID = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)

# Frozen copy of the freshness-monitor's default (config-I3086):
# ``infrastructure/lambdas/freshness-monitor/index.py`` line 226.
# After this many consecutive confirmed-miss sweeps, a ``severity=warning``
# row pages via the critical path even though check_results.json still
# carries ``severity: "warning"``.  Changing this constant in the Lambda
# without updating this test is a sign the two planes may have diverged.
_WARNING_ESCALATION_RUNS = 3

# The alerting states that trigger a page in ``_maybe_alert``.
_ALERTING_STATES = frozenset({"missing", "stale", "probe_failed"})

# The bad states the Fleet Status resolver treats as "not fresh".
_BAD_STATES = frozenset({"stale", "missing", "probe_failed"})


def _inputs(now=TRADING_MID, **kw) -> FleetInputs:
    return FleetInputs(now=now, is_trading_day=True, **kw)


def _cr(rows) -> dict:
    return {"run_at": TRADING_MID.isoformat(), "results": rows}


def _row(
    artifact_id: str,
    state: str,
    severity: str = "warning",
    consecutive_miss_runs: int = 0,
) -> dict:
    """Build a check_results.json row matching the freshness-monitor's
    ``_serialize_check_results`` output schema.

    Only the fields this test's alert-path model reads are set; the rest
    (owner_repo, canonical_key, reason, etc.) are filled with safe defaults
    that the Fleet Status resolver and the model both tolerate.
    """
    return {
        "artifact_id": artifact_id,
        "state": state,
        "severity": severity,
        "owner_repo": "nousergon/test",
        "canonical_key": f"artifacts/{artifact_id}.json",
        "reason": "",
        "consecutive_miss_runs": consecutive_miss_runs,
    }


# ── Alert-path model ────────────────────────────────────────────────────────
#
# Models ``_maybe_alert`` from the freshness-monitor at enough fidelity to
# decide whether a check_results entry WOULD have paged.  Discrepancies
# between this model and the real Lambda are themselves divergence signals
# that the test should escalate, not suppress; the model is intentionally
# conservative (favor false-negatives that Brian can investigate, never
# false-divergence alarms that desensitize).


def _would_page(row: dict) -> bool:
    """True when the freshness-monitor's ``_maybe_alert`` would fire for this
    check_results row, given the row's own severity and miss-run counter.

    Mirrors ``_maybe_alert`` at index.py:1026-1136: the row must be in an
    alerting state past SLA grace (simplified: any ``_ALERTING_STATES`` row
    has ``sla_violated_by_minutes > 0`` by construction once it's persisted
    as such — the model does NOT re-derive the grace-window arithmetic,
    matching the controller's ``_is_confirmed_miss`` gate) and the effective
    severity must be ``critical`` either statically or via warning escalation.
    """
    state = row.get("state")
    if state not in _ALERTING_STATES:
        return False

    # Static severity trumps — if already critical, page unconditionally
    # (the ``probe_failed`` rule in ``_maybe_alert`` coerces to critical
    # regardless of the registry severity; for simplicity, any row whose
    # *persisted* severity is critical is treated as a page — probe_failed
    # always persists with its coerced severity, and champion-arm-coerced
    # rows already carry ``severity: "critical"`` in check_results via
    # ``apply_dynamic_severity`` before serialization).
    severity = row.get("severity", "warning")
    if severity == "critical":
        return True

    # Warning escalation: a ``severity=warning`` row with enough consecutive
    # confirmed misses pages through the critical path even though its
    # persisted severity stays ``warning``.  This is the exact divergence
    # the chokepoint exists to detect.
    if severity == "warning":
        miss_runs = row.get("consecutive_miss_runs", 0)
        if miss_runs >= _WARNING_ESCALATION_RUNS:
            return True

    return False


def _would_not_page(row: dict) -> bool:
    return not _would_page(row)


# ── Tests ─────────────────────────────────────────────────────────────────--


class TestSurfaceParity:
    """Every artifact that the freshness-monitor pages must correspond to a
    console RED (or at minimum YELLOW — a page is never silent on the
    console).  Tests in this class model the alert path independently and
    cross-reference the resolved Fleet Status, catching ANY divergence
    between the two decision trees."""

    # ── Agreement cases (page → RED) ─────────────────────────────────────

    def test_critical_missing_pages_and_is_red(self):
        """A critical+missing artifact pages: Fleet Status agrees (RED)."""
        rows = [_row("a1", "missing", severity="critical")]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert _would_page(rows[0])
        assert s.dot == RED, (
            f"critical+missing artifact pages but Fleet Status shows {s.dot}: {s.reason}"
        )

    def test_critical_stale_pages_and_is_red(self):
        """A critical+stale artifact pages: Fleet Status agrees (RED)."""
        rows = [_row("a1", "stale", severity="critical")]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert _would_page(rows[0])
        assert s.dot == RED, (
            f"critical+stale artifact pages but Fleet Status shows {s.dot}: {s.reason}"
        )

    def test_critical_probe_failed_pages_and_is_red(self):
        """A critical+probe_failed artifact pages: Fleet Status agrees (RED)."""
        rows = [_row("a1", "probe_failed", severity="critical")]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert _would_page(rows[0])
        assert s.dot == RED, (
            f"critical+probe_failed artifact pages but Fleet Status shows {s.dot}: {s.reason}"
        )

    def test_fresh_does_not_page_and_not_red(self):
        """Fresh artifacts do not page: Fleet Status correctly not RED."""
        rows = [_row("a1", "fresh"), _row("a2", "fresh")]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        for r in rows:
            assert _would_not_page(r), f"fresh artifact unexpectedly pages: {r}"
        assert s.dot != RED, (
            f"all-fresh check_results shows RED: {s.reason}"
        )

    def test_grace_only_does_not_page_and_yellow(self):
        """Critical artifact in grace period does not page (within SLA grace)
        and Fleet Status correctly shows YELLOW (not RED)."""
        rows = [_row("a1", "grace_period", severity="critical")]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert _would_not_page(rows[0])
        assert s.dot == YELLOW, (
            f"grace-period artifact resolved to {s.dot}: {s.reason}"
        )

    # ── Divergence detection — the config-I3086 warning-escalation gap ────

    def test_escalated_warning_misses_page_but_fleet_status_not_red(self):
        """KNOWN DIVERGENCE (config-I3086 / config#3208): a severity=warning
        artifact confirmed-missing for ``WARNING_ESCALATION_RUNS`` consecutive
        sweeps pages via the critical path (config-I3086 warning escalation),
        but Fleet Status's ``resolve_artifact_freshness`` sees only the
        persisted ``severity: "warning"`` and shows YELLOW, never RED.

        This test asserts the KNOWN divergence: the page fires but the console
        does NOT show RED.  It documents the gap; a future fix (persisting
        the escalated effective severity in check_results.json and teaching
        the resolver to read it) should flip the expectation from
        ``assert s.dot != RED`` to ``assert s.dot == RED``.

        This is the exact failure shape that led to the 2026-07-13
        stale-champion-feed incident (config-I3053) — a confirmed miss that
        stayed console-only for days until a downstream pipeline hard-failed.
        """
        rows = [
            _row(
                "a1", "missing", severity="warning",
                consecutive_miss_runs=_WARNING_ESCALATION_RUNS,
            ),
        ]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        # The alert path pages:
        assert _would_page(rows[0]), (
            f"warning artifact with {_WARNING_ESCALATION_RUNS}+ consecutive misses "
            "should have been escalated to critical via config-I3086"
        )
        # The console does NOT agree — this is the documented gap:
        assert s.dot != RED, (
            "WARNING: escalated warning now shows RED.  The surface-parity gap "
            "may have been fixed — remove this assertion and consolidate the test."
        )
        assert s.dot == YELLOW, (
            f"escalated warning resolved to {s.dot}, expected YELLOW (the "
            f"persisted-severity gap): {s.reason}"
        )

    def test_zero_miss_warning_does_not_page(self):
        """A severity=warning artifact with zero consecutive misses does NOT
        page (no escalation) and Fleet Status correctly shows YELLOW.
        Baseline: distinguishes the previous test's result from the ordinary
        warning-handling path."""
        rows = [
            _row("a1", "missing", severity="warning", consecutive_miss_runs=0),
        ]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert _would_not_page(rows[0]), (
            "zero-miss warning should not escalate"
        )
        assert s.dot == YELLOW, (
            f"zero-miss warning resolved to {s.dot}: {s.reason}"
        )

    def test_escalated_warning_with_fresh_fleet_wide(self):
        """Mixed scenario: one escalated warning (pages) and one healthy
        critical artifact.  The escalated row's page has no RED counterpart,
        while the critical artifact correctly drives RED.  This demonstrates
        the divergence IS scoped to the warning-escalation gap and does not
        affect the wider page→RED agreement for statically-critical rows."""
        rows = [
            _row("a1", "missing", severity="critical"),          # pages, should be RED
            _row("a2", "mixed_no_alert", severity="fresh"),       # no page
            _row(
                "a3", "missing", severity="warning",              # pages (escalated),
                consecutive_miss_runs=_WARNING_ESCALATION_RUNS,    # but stays YELLOW
            ),
        ]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert _would_page(rows[0])
        assert _would_page(rows[2])
        assert _would_not_page(rows[1])
        # Fleet Status sees the statically-critical a1 and goes RED.
        # The escalated a3 is invisible to it.
        assert s.dot == RED, (
            f"mixed check_results resolved to {s.dot}: {s.reason}"
        )
        # Verify the RED is from a1 (critical), not a3 (escalated warning).
        assert any(
            d.get("artifact") == "a1"
            for d in s.detail
        ), "critical artifact a1 should be in the RED expander detail"

    # ── Edge cases ───────────────────────────────────────────────────────

    def test_no_check_results_artifact_is_gray(self):
        """When no check_results artifact exists at all, Fleet Status shows
        GRAY and the alert path has nothing to decide on — no divergence
        possible."""
        s = resolve_artifact_freshness(_inputs(check_results=None))
        assert s.dot != RED, "no check_results must not show RED"
        assert s.dot != YELLOW, "no check_results must not show YELLOW (unreachable in practice)"

    def test_empty_results_does_not_page(self):
        """An empty check_results (no artifacts checked) is not an alertable
        state — the Fleet Status should handle it gracefully."""
        s = resolve_artifact_freshness(_inputs(check_results=_cr([])))
        assert s.dot != RED, f"empty results resolved to {s.dot}"
