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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fleet_status import (  # noqa: E402
    RED,
    YELLOW,
    FleetInputs,
    GroomSnapshot,
    ModuleHealthRow,
    PipelineSnapshot,
    resolve_artifact_freshness,
    resolve_groomer,
    resolve_module_self_reports,
    resolve_pipeline,
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


class TestSurfaceParityV2RedToPage:
    """Reverse direction of the surface-parity chokepoint (config#3952,
    ARCHITECTURE.md §118 rule 5): every Fleet Status RED must have a
    corresponding page in the alert plane.

    For each Fleet Status component that resolves to RED, verify the
    matching alert mechanism (freshness-monitor, sf-telegram-notifier,
    watch dispatch) would have paged.  When no direct page path exists
    for a component, document the gap explicitly — same pattern as v1's
    ``test_escalated_warning_misses_page_but_fleet_status_not_red``.

    Unlike v1, which models a single alert path (freshness-monitor) for a
    single component (artifact_freshness), v2 must model MULTIPLE alert
    paths — different RED components page through different mechanisms.
    The page-path models below are intentionally conservative (matching
    v1's design philosophy: favor false-negatives that can be investigated,
    never false-divergence alarms that desensitise).

    chokepoint-identity: surface-parity-v2  (config#3952)
    """

    # ── Alert-path models ───────────────────────────────────────────────

    @staticmethod
    def _sf_telegram_would_page(snap: PipelineSnapshot) -> bool:
        """True when the sf-telegram-notifier would have paged for a Step
        Function execution in this state.

        Models ``_severity_for_status`` from
        ``infrastructure/lambdas/sf-telegram-notifier/index.py``: a Lambda
        that subscribes to Step Function execution events and pages loud
        (``send_loud``) for any terminal failure — ``FAILED``, ``TIMED_OUT``,
        or ``ABORTED``.  ``SUCCEEDED`` executions never page, even when the
        pipeline resolver's composite ``verdict`` is ``PARTIAL``/``FAILED``
        (a known divergence — see
        ``test_pipeline_succeeded_but_verdict_failed_red_no_page``).
        """
        return snap.status in {"FAILED", "TIMED_OUT", "ABORTED"}

    # ── artifact_freshness RED → freshness-monitor page ────────────────

    def test_artifact_freshness_red_implies_each_critical_rows_pages(self):
        """Every artifact_freshness RED is backed by rows where
        severity=critical and state∈{missing,stale,probe_failed}.  Each such
        row would also have paged through the freshness-monitor (the same
        ``_would_page`` model v1 uses — this test is the symmetric
        counterpart to v1's page→RED tests, verifying the direction holds
        from the console side as well)."""
        rows = [
            _row("a1", "missing", severity="critical"),
            _row("a2", "stale", severity="critical"),
            _row("a3", "probe_failed", severity="critical"),
            _row("a4", "fresh"),
        ]
        s = resolve_artifact_freshness(_inputs(check_results=_cr(rows)))
        assert s.dot == RED, f"critical-bad artifacts should be RED: {s.reason}"
        for r in rows[:3]:
            assert _would_page(r), f"critical artifact does not page: {r}"
        assert _would_not_page(rows[3]), "fresh artifact unexpectedly pages"

    # ── pipeline FAILED/TIMED_OUT RED → sf-telegram-notifier page ─────

    def test_pipeline_failed_red_pages_via_sf_telegram(self):
        """A pipeline whose Step Function execution FAILED shows RED on the
        console and pages loud through sf-telegram-notifier."""
        snap = PipelineSnapshot(
            status="FAILED",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1),
        )
        # weekly on a Tuesday = not expected → no overdue YELLOW
        inp = _inputs(pipelines={"weekly": snap})
        s = resolve_pipeline("weekly", inp)
        assert s.dot == RED, f"FAILED pipeline expected RED: {s.reason}"
        assert self._sf_telegram_would_page(snap), (
            "sf-telegram-notifier must page on FAILED execution"
        )

    def test_pipeline_timed_out_red_pages_via_sf_telegram(self):
        """A TIMED_OUT execution follows the same page path as FAILED."""
        snap = PipelineSnapshot(
            status="TIMED_OUT",
            started_at=TRADING_MID - timedelta(hours=2),
        )
        inp = _inputs(pipelines={"weekly": snap})
        s = resolve_pipeline("weekly", inp)
        assert s.dot == RED, f"TIMED_OUT pipeline expected RED: {s.reason}"
        assert self._sf_telegram_would_page(snap), (
            "sf-telegram-notifier must page on TIMED_OUT execution"
        )

    # ── pipeline SUCCEEDED+FAILED_verdict RED — sf-telegram knows nothing

    def test_pipeline_succeeded_but_verdict_failed_red_no_page(self):
        """KNOWN DIVERGENCE: a Step Function that SUCCEEDED at the execution
        level but whose artifact-completion check produced a FAILED verdict
        is RED on the Fleet Status console but the sf-telegram-notifier does
        NOT page — it only sees the execution ``status: SUCCEEDED`` and
        does not look at the composite ``verdict`` field.

        This is a real surface-parity gap (config#3952): the console says
        ``last cycle FAILED`` (RED) for a pipeline whose Step Function
        actually reached SUCCEEDED, but the pager saw SUCCEEDED and stayed
        silent.  The missing-artifact alert for this case comes through the
        **freshness-monitor** (artifact_freshness component), not the
        pipeline resolver — so the parity guarantee across BOTH planes
        depends on artifact_freshness also catching the failure.  The
        artifact_freshness RED→page test above covers that direction.

        This test asserts the divergence: console RED, no sf-telegram page.
        If a future change makes the notifier also subscribe to composite
        verdicts, this test marks where to flip the assertion and consolidate.
        """
        snap = PipelineSnapshot(
            status="SUCCEEDED",
            verdict="FAILED",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1),
        )
        inp = _inputs(pipelines={"weekly": snap})
        s = resolve_pipeline("weekly", inp)
        assert s.dot == RED, (
            f"SUCCEEDED+FAILED verdict expected RED, got {s.dot}: {s.reason}"
        )
        # sf-telegram-notifier does NOT page:
        assert not self._sf_telegram_would_page(snap), (
            "sf-telegram-notifier must NOT page on SUCCEEDED execution "
            "(the verdict gap this test documents)"
        )

    def test_pipeline_succeeded_complete_no_red_no_page(self):
        """A SUCCEEDED pipeline with COMPLETE verdict is the happy path:
        not RED, no sf-telegram page.  Baseline that distinguishes the
        previous test."""
        snap = PipelineSnapshot(
            status="SUCCEEDED",
            verdict="COMPLETE",
            started_at=TRADING_MID - timedelta(hours=2),
            stopped_at=TRADING_MID - timedelta(hours=1),
        )
        inp = _inputs(pipelines={"weekly": snap})
        s = resolve_pipeline("weekly", inp)
        assert s.dot != RED, (
            f"SUCCEEDED+COMPLETE should not be RED: {s.reason}"
        )
        assert not self._sf_telegram_would_page(snap), (
            "sf-telegram-notifier must not page on SUCCEEDED+COMPLETE"
        )

    # ── Known gaps — components RED but no direct page path ────────────

    def test_known_gap_backlog_groomer_red_no_page(self):
        """KNOWN GAP: backlog_groomer RED (stale in-progress marker without
        a live spot instance, or idle past GROOM_IDLE_WARN) has NO page path.
        The groomer is a best-effort batch job, not a live service with an
        SLA — its failures do not page.  This is a deliberate architectural
        choice, not an oversight, but it IS a divergence from strict surface
        parity (config#3952).

        If a page path is added in the future (e.g. an alarm on the groom
        spot failing to start), flip this assertion from ``assert s.dot == RED``
        plus a gap comment to ``assert s.dot != RED or <page-would-fire>``."""
        g = GroomSnapshot(
            marker_started_at=TRADING_MID - timedelta(hours=48),
            spot_running=False,
        )
        inp = _inputs(groom=g)
        s = resolve_groomer(inp)
        assert s.dot == RED, f"stale groom marker expected RED: {s.reason}"
        # No page path exists — this is the documented gap.

    def test_known_gap_module_self_reports_failed_no_page(self):
        """KNOWN GAP: module_self_reports RED (module reports 'failed') has
        NO independent page path.  Health self-reports are enrichment data
        per config#1724 (self-report is enrichment, never the authority) —
        the authority for module health is the freshness-monitor's independent
        artifact probes, not the module's own report.  A module whose health
        check says 'failed' but whose artifacts are still fresh is not paged
        (the artifacts ARE the real page signal)."""
        rows = (
            ModuleHealthRow(
                module="crucible-executor", status="failed",
                age_hrs=0.5, stale_after_hrs=2.0,
            ),
        )
        inp = _inputs(module_health=rows)
        s = resolve_module_self_reports(inp)
        assert s.dot == RED, f"failed module expected RED: {s.reason}"
        # No independent page path — documented gap.
