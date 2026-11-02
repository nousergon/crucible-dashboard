"""`claude-assistant.yml` (reusable) must follow the fleet runner switch, for private callers only.

alpha-engine-config-I12850: the callee's `runs-on` input defaulted to
`ubuntu-latest`, so a private caller that passed nothing (metron-ops,
vires-ops) stayed on the hosted allowance after a `CI_RUNNER_MODE=codebuild`
flip. The default now follows the rule nousergon-lib's notify-ci-failure.yml
(PR517) and provider-linkage-guard.yml (PR518) use; an explicit input still
wins. Public callers (this repo, crucible-*) have no CodeBuild runner and
must stay hosted, since `vars` resolves in the caller's context.

The expression is evaluated by translating GitHub's value-returning `&&`/`||`
into Python's `and`/`or`, so the cases exercise it as written.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "claude-assistant.yml"
)
PRIVATE = "codebuild-nousergon-private-runner-123-2"
TOGGLE = "codebuild-alpha-engine-config-runner-123-2"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


class _Null:
    def __bool__(self) -> bool:
        return False

    def __eq__(self, _other: object) -> bool:
        return False

    def __getattr__(self, _name: str) -> _Null:
        return self


class _Absent(SimpleNamespace):
    def __getattr__(self, _name: str) -> _Null:
        return _Null()


def _eval(
    expr: str, *, runs_on_input: str, mode: str | None, repository: dict | None
) -> str:
    body = expr.strip()
    assert body.startswith("${{") and body.endswith("}}"), body
    py = body[3:-2].strip().replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"\btrue\b", "True", py)
    py = (
        py.replace("inputs.runs-on", "inputs_runs_on")
        .replace("vars.", "vars_.")
        .replace("format(", "fmt(")
    )

    def fmt(template: str, *args: object) -> str:
        for i, a in enumerate(args):
            template = template.replace(f"{{{i}}}", str(a))
        return template

    repo = _Absent(**repository) if repository is not None else None
    github = SimpleNamespace(
        event=_Absent(repository=repo) if repo is not None else _Absent(),
        run_id=123,
        run_attempt=2,
    )
    vars_ = _Absent(CI_RUNNER_MODE=mode) if mode is not None else _Absent()
    return eval(
        py,
        {"__builtins__": {}},
        {"inputs_runs_on": runs_on_input, "vars_": vars_, "github": github, "fmt": fmt},
    )


def test_default_input_is_empty_so_the_switch_decides():
    on = _load().get("on", _load().get(True))
    assert on["workflow_call"]["inputs"]["runs-on"]["default"] == ""


@pytest.mark.parametrize(
    ("runs_on_input", "mode", "repository", "expected"),
    [
        ("", "codebuild", {"name": "metron-ops", "private": True}, PRIVATE),
        ("", "codebuild", {"name": "vires-ops", "private": True}, PRIVATE),
        ("", "gha", {"name": "metron-ops", "private": True}, "ubuntu-latest"),
        ("", None, {"name": "metron-ops", "private": True}, "ubuntu-latest"),
        (
            "",
            "codebuild",
            {"name": "crucible-dashboard", "private": False},
            "ubuntu-latest",
        ),
        ("", "codebuild", {"name": "crucible-executor"}, "ubuntu-latest"),
        ("", "codebuild", None, "ubuntu-latest"),
        (TOGGLE, "codebuild", {"name": "alpha-engine-config", "private": True}, TOGGLE),
        (
            "ubuntu-latest",
            "codebuild",
            {"name": "alpha-engine-config", "private": True},
            "ubuntu-latest",
        ),
    ],
    ids=[
        "private-codebuild",
        "private-codebuild-shared-project",
        "private-gha",
        "mode-unset",
        "public-codebuild-stays-hosted",
        "private-field-absent-stays-hosted",
        "no-repository-payload-stays-hosted",
        "explicit-input-wins",
        "explicit-hosted-input-wins",
    ],
)
def test_runs_on(runs_on_input, mode, repository, expected):
    expr = _load()["jobs"]["respond"]["runs-on"]
    assert (
        _eval(expr, runs_on_input=runs_on_input, mode=mode, repository=repository)
        == expected
    )
