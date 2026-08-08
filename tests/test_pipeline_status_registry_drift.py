"""
tests/test_pipeline_status_registry_drift.py — Walk the live SF JSONs
(alpha-engine-data/infrastructure/{step_function,step_function_daily,
step_function_eod}.json) and assert every substantive Task state has a
registry entry in ``nousergon_lib.pipeline_status.registry``.

This is the cross-repo invariant guard called out in the registry's
docstring: ``"A CI test in the consuming repo (alpha-engine-dashboard
or alpha-engine-data) asserts every substantive Task state in the live
SF JSONs has a registry entry; that's how the two stay in sync without
a runtime coupling."``

The data SF JSONs live in a sibling checkout (~/Development/alpha-engine-data/).
If that checkout isn't present (CI machine, fresh clone), the test
SKIPs rather than fails — the invariant only needs to hold on a dev
laptop or in the dashboard-side CI environment that does the cross-repo
walk. (Phase 3 of the revamp will land the same guard on the
alpha-engine-data side as part of its SF JSON edits.)

What constitutes a "substantive Task state" here:
  - Type == "Task"
  - Resource ARN ∈ SUBSTANTIVE_RESOURCES (sns:publish / lambda:invoke /
    ssm:sendCommand / ec2:startInstances / ec2:stopInstances)
  - NOT a Wait companion (those roll up into their parent per WAIT_GROUPING)
  - NOT a ``getCommandInvocation`` polling state (those ARE Wait companions,
    just named differently in the JSON)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nousergon_lib.pipeline_status.registry import (
    STATE_TO_ARCHIVE_PAGE,
    SUBSTANTIVE_RESOURCES,
    WAIT_GROUPING,
)


# Sibling checkout convention — adjust if the layout changes.
_SIBLING_DATA_REPO = Path.home() / "Development" / "alpha-engine-data"
_SF_JSON_FILES = [
    ("Saturday", _SIBLING_DATA_REPO / "infrastructure" / "step_function.json"),
    ("Weekday", _SIBLING_DATA_REPO / "infrastructure" / "step_function_daily.json"),
    ("EOD", _SIBLING_DATA_REPO / "infrastructure" / "step_function_eod.json"),
]


# Polling Wait companions use ``getCommandInvocation`` — never substantive.
_POLLING_RESOURCE = "arn:aws:states:::aws-sdk:ssm:getCommandInvocation"


def _walk_substantive_task_states(states: dict, found: set) -> set:
    """Walk SF JSON ``States`` map, descending into Parallel + Map branches,
    and collect every Task state name whose Resource is in
    SUBSTANTIVE_RESOURCES.

    Returns a set of state names. The walk is post-order (no order matters
    for the equality check downstream)."""
    for name, body in states.items():
        if not isinstance(body, dict):
            continue
        type_ = body.get("Type")
        if type_ == "Task":
            resource = body.get("Resource")
            if isinstance(resource, str) and resource in SUBSTANTIVE_RESOURCES:
                found.add(name)
        elif type_ == "Parallel":
            for branch in body.get("Branches", []):
                _walk_substantive_task_states(branch.get("States", {}), found)
        elif type_ == "Map":
            iterator = body.get("Iterator") or body.get("ItemProcessor", {})
            _walk_substantive_task_states(iterator.get("States", {}), found)
    return found


def _all_substantive_states(json_path: Path) -> set:
    sf = json.loads(json_path.read_text())
    return _walk_substantive_task_states(sf.get("States", {}), set())


@pytest.mark.parametrize("label,json_path", _SF_JSON_FILES)
def test_every_substantive_state_has_registry_entry(label, json_path):
    """The load-bearing cross-repo invariant. If this fails, the dashboard
    page 25 will render a "⚠️ Registry drift" cell for the missing state —
    visible-but-degraded. Fix: add the new state name + ArchivePageRef or
    ArtifactReason to ``nousergon_lib.pipeline_status.registry`` and
    bump the lib version."""
    if not json_path.exists():
        pytest.skip(
            f"{label} SF JSON not present at {json_path} — sibling alpha-engine-data "
            f"checkout missing. Test skips on CI machines without the cross-repo "
            f"layout; runs on dev laptops + the dashboard CI environment."
        )

    substantive = _all_substantive_states(json_path)
    # Wait companions roll up into their parent row per WAIT_GROUPING (the
    # docstring's "NOT a Wait companion" rule). Historically they were
    # excluded implicitly because they polled via getCommandInvocation (not
    # a substantive Resource); the ssm-liveness-poller rewiring (config#1811,
    # 2026-07-06) made poll iterations lambda:invoke Tasks, so the exclusion
    # must be explicit — a WAIT_GROUPING member never needs its own
    # registry entry (read._absorb_wait_companion folds it before lookup).
    substantive -= set(WAIT_GROUPING.keys())
    missing = substantive - set(STATE_TO_ARCHIVE_PAGE.keys())

    assert not missing, (
        f"{label} SF has {len(missing)} substantive Task state(s) NOT in "
        f"nousergon_lib.pipeline_status.registry.STATE_TO_ARCHIVE_PAGE: "
        f"{sorted(missing)}. Add each one to the registry with an ArchivePageRef "
        f"deep-link OR an explicit ArtifactReason string, then bump the lib version."
    )


@pytest.mark.parametrize("label,json_path", _SF_JSON_FILES)
def test_wait_companions_in_json_are_in_wait_grouping(label, json_path):
    """Every state named ``WaitFor*`` in the SF JSON must appear in
    WAIT_GROUPING — otherwise the Wait state would render as its own row
    instead of rolling into its parent."""
    if not json_path.exists():
        pytest.skip(f"{label} SF JSON not present at {json_path}")

    sf = json.loads(json_path.read_text())

    def _collect_wait_states(states: dict, found: set) -> set:
        for name, body in states.items():
            if not isinstance(body, dict):
                continue
            if name.startswith("WaitFor"):
                found.add(name)
            if body.get("Type") == "Parallel":
                for branch in body.get("Branches", []):
                    _collect_wait_states(branch.get("States", {}), found)
            elif body.get("Type") == "Map":
                iterator = body.get("Iterator") or body.get("ItemProcessor", {})
                _collect_wait_states(iterator.get("States", {}), found)
        return found

    wait_states = _collect_wait_states(sf.get("States", {}), set())
    missing = wait_states - set(WAIT_GROUPING.keys())

    assert not missing, (
        f"{label} SF has {len(missing)} ``WaitFor*`` state(s) NOT in "
        f"nousergon_lib.pipeline_status.registry.WAIT_GROUPING: "
        f"{sorted(missing)}. Each must map to its parent Task state name; "
        f"otherwise the wait companion will render as its own row instead of "
        f"rolling up."
    )


# ── Weekly-SF live progress strip (config-I2966) ────────────────────────────
#
# The strip (fleet_status.WEEKLY_SF_STRIP_STATES / build_weekly_sf_strip)
# has its own ordered state list — necessarily separate from
# STATE_TO_ARCHIVE_PAGE (a registry is an unordered dict; the strip needs
# SF topology/order, which the lib deliberately does not encode). The
# acceptance criterion for config-I2966 is that this second list can never
# silently drift from either (a) the lib registry, or (b) the live weekly
# SF JSON's own substantive-state set — both directions are asserted below,
# mirroring this file's existing pattern for STATE_TO_ARCHIVE_PAGE itself.

_WEEKLY_SF_JSON = _SIBLING_DATA_REPO / "infrastructure" / "step_function.json"

# ResearchPredictorParallel's two Parallel branches, by index, in the live
# weekly SF JSON (Branch 0 = Research/"Branch A" — carries RAGIngestion;
# Branch 1 = PredictorTraining/"Branch B") — mirrors fleet_status.py's own
# WEEKLY_SF_BRANCH_A_STEPS / WEEKLY_SF_BRANCH_B_STEPS split.
_PARALLEL_STATE_NAME = "ResearchPredictorParallel"
_BRANCH_A_INDEX = 0
_BRANCH_B_INDEX = 1


def _walk_substantive_states_flat(states: dict, found: set) -> set:
    """Like ``_walk_substantive_task_states`` but does NOT descend into
    Parallel branches — used to collect the linear backbone only (the
    Parallel node itself is skipped; its branches are walked separately by
    ``_branch_substantive_states``)."""
    for name, body in states.items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") == "Task":
            resource = body.get("Resource")
            if isinstance(resource, str) and resource in SUBSTANTIVE_RESOURCES:
                found.add(name)
        elif body.get("Type") == "Map":
            iterator = body.get("Iterator") or body.get("ItemProcessor", {})
            _walk_substantive_states_flat(iterator.get("States", {}), found)
        # Deliberately skip "Parallel" here — ResearchPredictorParallel's
        # branches are the strip's two separate lanes, walked below.
    return found


def _branch_substantive_states(sf: dict, parallel_name: str, branch_index: int) -> set:
    parallel = sf["States"][parallel_name]
    branch = parallel["Branches"][branch_index]
    return _walk_substantive_task_states(branch.get("States", {}), set())


@pytest.fixture(scope="module")
def _weekly_sf_json_or_skip():
    if not _WEEKLY_SF_JSON.exists():
        pytest.skip(
            f"Weekly SF JSON not present at {_WEEKLY_SF_JSON} — sibling "
            f"alpha-engine-data checkout missing."
        )
    return json.loads(_WEEKLY_SF_JSON.read_text())


def test_weekly_sf_strip_states_are_all_registered():
    """Every state named in fleet_status.WEEKLY_SF_STRIP_STATES must be a
    real STATE_TO_ARCHIVE_PAGE key — the strip can only show a chip whose
    data provenance the lib registry already vouches for. Runs with no
    sibling-repo dependency (pure lib-vs-dashboard-constant check)."""
    from fleet_status import WEEKLY_SF_STRIP_STATES

    missing = set(WEEKLY_SF_STRIP_STATES) - set(STATE_TO_ARCHIVE_PAGE.keys())
    assert not missing, (
        f"fleet_status.WEEKLY_SF_STRIP_STATES names {len(missing)} state(s) "
        f"NOT in nousergon_lib.pipeline_status.registry.STATE_TO_ARCHIVE_PAGE: "
        f"{sorted(missing)}. Add each to the lib registry (bump the lib "
        f"version) before adding it to the strip."
    )


def test_weekly_sf_strip_backbone_matches_live_sf_json(_weekly_sf_json_or_skip):
    """The strip's pre-/post-parallel linear backbone must be exactly the
    live weekly SF JSON's own linear substantive states (excluding the two
    ResearchPredictorParallel branches, walked separately below) — a new
    backbone state added to the SF without a strip update fails here."""
    from fleet_status import (
        WEEKLY_SF_POST_PARALLEL_STEPS,
        WEEKLY_SF_PRE_PARALLEL_STEPS,
    )

    sf = _weekly_sf_json_or_skip
    live_backbone = _walk_substantive_states_flat(sf.get("States", {}), set())
    strip_backbone = set(WEEKLY_SF_PRE_PARALLEL_STEPS) | set(WEEKLY_SF_POST_PARALLEL_STEPS)

    missing = live_backbone - strip_backbone
    assert not missing, (
        f"Weekly SF's linear backbone has {len(missing)} substantive "
        f"state(s) NOT on the progress strip: {sorted(missing)}. Add each "
        f"to fleet_status.WEEKLY_SF_PRE_PARALLEL_STEPS or "
        f"WEEKLY_SF_POST_PARALLEL_STEPS in the state's actual SF position."
    )
    stale = strip_backbone - live_backbone
    assert not stale, (
        f"Progress strip names {len(stale)} backbone state(s) no longer in "
        f"the live weekly SF JSON: {sorted(stale)}. Remove from "
        f"fleet_status.py's WEEKLY_SF_PRE_PARALLEL_STEPS / "
        f"WEEKLY_SF_POST_PARALLEL_STEPS (Change-Management Standard §113 — "
        f"a removal must not leave a stale strip reference behind)."
    )


def test_weekly_sf_strip_branch_a_matches_research_branch(_weekly_sf_json_or_skip):
    """Branch A (Research, carries RAGIngestion) must match
    ResearchPredictorParallel's Branch 0 substantive states exactly."""
    from fleet_status import WEEKLY_SF_BRANCH_A_STEPS

    sf = _weekly_sf_json_or_skip
    live = _branch_substantive_states(sf, _PARALLEL_STATE_NAME, _BRANCH_A_INDEX)
    strip = set(WEEKLY_SF_BRANCH_A_STEPS)
    missing = live - strip
    stale = strip - live
    assert not missing, (
        f"ResearchPredictorParallel Branch 0 (Research) has {len(missing)} "
        f"substantive state(s) not on the strip's Branch A lane: "
        f"{sorted(missing)}. Add to fleet_status.WEEKLY_SF_BRANCH_A_STEPS."
    )
    assert not stale, (
        f"Strip's Branch A lane names {len(stale)} state(s) no longer in "
        f"the live Research branch: {sorted(stale)}. Remove from "
        f"fleet_status.WEEKLY_SF_BRANCH_A_STEPS."
    )
    assert "RAGIngestion" in strip, (
        "Branch A must carry RAGIngestion — the strip's inner-step "
        "enrichment (config-I2966 deliverable #2) is keyed to this state."
    )


def test_weekly_sf_strip_branch_b_matches_predictor_branch(_weekly_sf_json_or_skip):
    """Branch B (PredictorTraining/model-zoo) must match
    ResearchPredictorParallel's Branch 1 substantive states exactly."""
    from fleet_status import WEEKLY_SF_BRANCH_B_STEPS

    sf = _weekly_sf_json_or_skip
    live = _branch_substantive_states(sf, _PARALLEL_STATE_NAME, _BRANCH_B_INDEX)
    strip = set(WEEKLY_SF_BRANCH_B_STEPS)
    missing = live - strip
    stale = strip - live
    assert not missing, (
        f"ResearchPredictorParallel Branch 1 (PredictorTraining) has "
        f"{len(missing)} substantive state(s) not on the strip's Branch B "
        f"lane: {sorted(missing)}. Add to "
        f"fleet_status.WEEKLY_SF_BRANCH_B_STEPS."
    )
    assert not stale, (
        f"Strip's Branch B lane names {len(stale)} state(s) no longer in "
        f"the live PredictorTraining branch: {sorted(stale)}. Remove from "
        f"fleet_status.WEEKLY_SF_BRANCH_B_STEPS."
    )
