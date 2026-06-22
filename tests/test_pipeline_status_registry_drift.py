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
