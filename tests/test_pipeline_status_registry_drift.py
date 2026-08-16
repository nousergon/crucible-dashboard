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

The data SF JSONs live in a sibling checkout (~/Development/nousergon-data/,
formerly alpha-engine-data — both names are tried, see below).
CI checks that repo out itself and points SF_DEFS_DIR at it, so this
guard RUNS on every pull request. A missing checkout is a skip on a
laptop and a FAILURE on a runner (alpha-engine-config-I7446): for its
whole life before that, this test skipped on every CI machine, which
made a cross-repo invariant hold only where someone happened to have
both repos cloned side by side.

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
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nousergon_lib.pipeline_status.registry import (
    STATE_TO_ARCHIVE_PAGE,
    SUBSTANTIVE_RESOURCES,
    WAIT_GROUPING,
)


# Sibling checkout convention. THE REPO WAS RENAMED (alpha-engine-data ->
# nousergon-data) and this path was not, which is worse than either a pass or a
# fail: on 2026-08-16 the laptop still held an ABANDONED alpha-engine-data
# checkout last written 2026-07-22, so this guard walked a three-week-old SF
# definition and reported drift ('Evaluator' has no registry entry) against a
# state the deployed Saturday SF has not had since it split into
# EvaluatorDiagnostics/EvaluatorOptimize. A stale checkout can just as easily
# report a PASS over a definition that has since drifted — same defect, silent.
#
# Ordered by preference, first existing wins: the current name, then the
# historical one (which is also what the box clones it as, per boot-pull.sh).
_SIBLING_DATA_REPO_CANDIDATES = [
    Path.home() / "Development" / "nousergon-data",
    Path.home() / "Development" / "alpha-engine-data",
]
# SF_DEFS_DIR is EXCLUSIVE when set — CI checks the data repo out and points it
# here (ci.yml, `test` job). Falling back to a laptop path when an explicitly
# named directory is missing is how a guard silently reads the wrong tree; if
# the caller named a location, that location is the answer or there isn't one.
_SIBLING_DATA_REPO = (
    Path(os.environ["SF_DEFS_DIR"])
    if os.environ.get("SF_DEFS_DIR")
    else next(
        (p for p in _SIBLING_DATA_REPO_CANDIDATES if p.is_dir()),
        _SIBLING_DATA_REPO_CANDIDATES[0],
    )
)

#: On a runner, a missing checkout is a BROKEN GUARD, not an absent laptop
#: (alpha-engine-config-I7446). This guard skipped on every CI machine for its
#: whole life, so the cross-repo invariant it enforces held only where someone
#: happened to have both repos cloned — and a skip is indistinguishable from a
#: pass in the summary line everyone reads.
_ON_CI = os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _require_definitions(label: str, json_path: Path) -> None:
    """Skip on a laptop without the sibling checkout; FAIL on a runner."""
    if json_path.exists():
        return
    message = (
        f"{label} SF JSON not present at {json_path}. CI checks the data repo "
        f"out and sets SF_DEFS_DIR (see ci.yml, `test` job); a dev laptop uses "
        f"~/Development/nousergon-data."
    )
    if _ON_CI:
        pytest.fail(
            f"{message} On CI this is a broken guard, not an absent layout — "
            f"skipping here would report a cross-repo invariant as satisfied "
            f"without ever evaluating it."
        )
    pytest.skip(message)


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
    _require_definitions(label, json_path)

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
    _require_definitions(label, json_path)

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
