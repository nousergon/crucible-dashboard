"""No file in this repo may create or update a CloudWatch alarm.

WHY
---
Alarm definitions for the dashboard box live in the PRIVATE `nous-ergon-ops`
repo under `infrastructure/cloudwatch/alarms/*.json`, applied by that repo's
`cloudwatch-alarm-apply-on-merge.yml`. Until 2026-08-21 this repo applied the
same eight `alpha-engine-dashboard-*` alarms too — two inline
`put-metric-alarm` calls in `.github/workflows/deploy.yml` and six more from
`infrastructure/install-host-alarms.sh`, which that workflow ran on every merge
to main.

Two appliers, one resource: the live alarm was whichever ran last. Measured
live 2026-08-21, all eight matched their codified JSON exactly — and that is
the dangerous state, not the safe one. Nothing goes red when a deliberate edit
in `nous-ergon-ops` is silently reverted by the next merge here; the daily
`cloudwatch-alarm-drift.yml` compares live against the JSON tree and would
report a MATCH the whole time, because the reverting writer is writing the same
values it always wrote. The divergence only appears at the moment someone tries
to change something, which is the moment they are least likely to suspect a
second writer.

`nous-ergon-ops/infrastructure/cloudwatch/README.md` had this named:

    | Creator | Applied by |
    | `alpha-engine-dashboard/infrastructure/install-host-alarms.sh` | `deploy.yml` |

listed under "Nothing requires an alarm's creator to write a file here".

THE INVERSION TAKEN HERE is `nousergon-data`'s (config-I7359, 2026-08-14): a
PUBLIC repo stops creating alarms rather than shallow-cloning the private repo
for the definitions. `nousergon-data/tests/test_no_imperative_alarm_authorship.py`
is this file's sibling and this repo's model.

SCOPE, stated because it bounds the guarantee: this is a source-text guard over
THIS repo. It cannot see an imperative applier in another repo, and it does not
prove the JSON tree is correct — only that this repo is not a second writer.
The tree's own correctness is `nous-ergon-ops`'s tests plus its daily drift job.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that hold executable/applied configuration. Docs and tests may
# discuss the API by name — this file does, at length — so they are excluded by
# construction rather than by an inline-comment carve-out that would let a real
# call hide behind a `#`.
_SEARCH_DIRS = ("infrastructure", ".github")

# The three mutating CloudWatch alarm APIs. `describe-alarms` and
# `get-metric-*` are reads and stay allowed: box_health.sh and the deploy
# scripts legitimately inspect alarm state.
_MUTATORS = ("put-metric-alarm", "put_metric_alarm",
             "put-composite-alarm", "put_composite_alarm",
             "set-alarm-state", "set_alarm_state")

# A shell/YAML comment line, or a Python comment line. A mention inside prose is
# not an applier; a call is. Matching on "the line is a comment" rather than on
# "the token appears" is what lets the retired install-host-alarms.sh keep its
# full rationale in the header without re-arming this guard.
_COMMENT = re.compile(r"^\s*#")


def _offending_lines():
    hits = []
    for d in _SEARCH_DIRS:
        root = REPO_ROOT / d
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".sh", ".yml", ".yaml", ".py"}:
                continue
            for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                if _COMMENT.match(line):
                    continue
                if any(m in line for m in _MUTATORS):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    return hits


def test_no_file_in_this_repo_creates_an_alarm():
    hits = _offending_lines()
    assert not hits, (
        "This repo may not author CloudWatch alarms — the definitions are "
        "nous-ergon-ops/infrastructure/cloudwatch/alarms/*.json and the applier "
        "is that repo's cloudwatch-alarm-apply-on-merge.yml "
        "(alpha-engine-config-I8035 / config-I7339). Two appliers for one alarm "
        "means the live value is whichever ran last, and the drift checker "
        "reports MATCH throughout because both write the same thing — until "
        "someone changes one of them.\n\nOffending lines:\n  "
        + "\n  ".join(hits)
    )


def test_install_host_alarms_is_a_pointer_not_an_applier():
    """The retired script must still exist, and must still say where to go.

    Deleting it outright was the alternative. Keeping it as a pointer is
    deliberate: `deploy-on-merge.sh` and the deploy-infra installer-routing test
    both reason about the infrastructure/*.sh set, and a name that vanishes
    takes its rationale with it. A stale muscle-memory invocation should be a
    no-op that tells you where the definitions went, not `command not found`.
    """
    p = REPO_ROOT / "infrastructure" / "install-host-alarms.sh"
    assert p.is_file(), "the pointer stub was deleted; see this test's docstring"
    body = p.read_text()
    assert "no longer creates alarms" in body
    assert "nous-ergon-ops/infrastructure/cloudwatch/alarms" in body
    assert "apply.py --prefix alpha-engine-dashboard-" in body, (
        "the stub must name the exact command an operator runs to apply a "
        "change immediately, or the pointer only says 'not here'"
    )


def test_deploy_workflow_no_longer_runs_the_alarm_installer():
    wf = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    for n, line in enumerate(wf.splitlines(), 1):
        if _COMMENT.match(line):
            continue
        assert "install-host-alarms.sh" not in line, (
            f"deploy.yml:{n} still invokes the retired alarm installer: {line.strip()}"
        )
