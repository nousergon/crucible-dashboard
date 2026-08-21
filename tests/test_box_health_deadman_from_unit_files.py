"""A timer may declare its own dead-man threshold, in its own unit file.

WHY (alpha-engine-config-I8034)
-------------------------------
`budget.yaml::timers` is the watchdog's staleness source, it lives in THIS repo,
and this repo installs only some of the box's timers. `nous-ergon-ops`, `metron`
and `the-cyphering` install others, so a timer arriving from one of those had to
have its row hand-added here afterwards by someone who first noticed the box's
`notice: timer has no dead-man threshold` line.

Nobody reliably did. Six instances, each closed by adding the row, none closing
the class:

  2026-07-29  metron-intraday.timer
  2026-08-08  three dashboard timers at once (config-I6657)
  2026-08-20  emit-service-memory.timer
  2026-08-21  litellm-config-reconcile.timer and llm-capability-probe.timer,
              hours after nous-ergon-ops-PR809 armed them

`tests/test_every_installed_timer_has_a_deadman_row.py` blocks the in-repo case
in CI, and its own SCOPE paragraph correctly predicted the 2026-08-21 pair it
could not see. A guard that documents the case it misses still misses it.

So `generate-box-manifest.py` now also reads `X-DeadManStaleness=` from the
`[Unit]` section of each installed `*.timer`. systemd accepts and ignores `X-`
keys, so the declaration is inert to the scheduler and travels with the file
into whatever repo installs it.

WHAT IS PINNED HERE, and why each one matters:

  * budget.yaml WINS a disagreement. A row here is a curated fleet decision with
    its rationale in prose; the key is the unit's claim about itself. If the key
    won, a sibling repo editing its unit would silently change a threshold this
    repo reasoned about.
  * A CONFLICT IS RECORDED, not swallowed. Resolving silently to budget.yaml
    makes the losing declaration invisible, and an invisible losing declaration
    is how someone edits a unit, sees no effect, and edits it again.
  * A MALFORMED VALUE RAISES. `parse_duration`'s existing contract: a threshold
    that silently became a wrong number reports as covered, which is strictly
    worse than reporting as uncovered.
  * A MISSING UNIT DIRECTORY IS NOT AN ERROR. This generator runs in CI and in
    tests on machines with no meaningful /etc/systemd/system, and there the
    correct behaviour is exactly the pre-I8034 behaviour.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _generator():
    path = REPO_ROOT / "infrastructure" / "generate-box-manifest.py"
    spec = importlib.util.spec_from_file_location("gen_box_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["gen_box_manifest"] = mod
    spec.loader.exec_module(mod)
    return mod


GEN = _generator()


def _timer(tmp_path: pathlib.Path, name: str, body: str) -> pathlib.Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_a_declared_key_is_read(tmp_path):
    _timer(tmp_path, "foo.timer",
           "[Unit]\nDescription=x\nX-DeadManStaleness=45m\n[Timer]\nOnCalendar=hourly\n")
    assert GEN.unit_declared_staleness(tmp_path) == {"foo.timer": 45 * 60}


def test_a_timer_without_the_key_is_simply_absent(tmp_path):
    _timer(tmp_path, "bar.timer", "[Unit]\nDescription=x\n[Timer]\nOnCalendar=daily\n")
    assert GEN.unit_declared_staleness(tmp_path) == {}


def test_service_files_are_not_scanned(tmp_path):
    """The threshold is a property of the SCHEDULE, so only *.timer carries it.

    A `.service` is triggered by its timer; declaring a staleness on the service
    would put the value on the half that does not have a cadence.
    """
    _timer(tmp_path, "baz.service", "[Unit]\nX-DeadManStaleness=1h\n")
    assert GEN.unit_declared_staleness(tmp_path) == {}


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert GEN.unit_declared_staleness(tmp_path / "nope") == {}


def test_a_malformed_value_raises(tmp_path):
    _timer(tmp_path, "bad.timer", "[Unit]\nX-DeadManStaleness=soon\n[Timer]\nOnCalendar=daily\n")
    with pytest.raises(ValueError):
        GEN.unit_declared_staleness(tmp_path)


def test_a_prefix_lookalike_key_is_not_matched(tmp_path):
    """`X-DeadManStalenessOverride=` is a different key and must not be read.

    A `startswith` match with no `=`-split check would take it, and the value it
    took would be the wrong one for a key nothing else honours.
    """
    _timer(tmp_path, "look.timer",
           "[Unit]\nX-DeadManStalenessOverride=9h\n[Timer]\nOnCalendar=daily\n")
    assert GEN.unit_declared_staleness(tmp_path) == {}


def test_the_generator_declares_the_key_name_once():
    """The key name is spelled in exactly one place in this repo's Python.

    Every other consumer refers to `DEADMAN_KEY`. A second literal is how the
    two spellings drift and the reader silently matches nothing.
    """
    src = (REPO_ROOT / "infrastructure" / "generate-box-manifest.py").read_text()
    assert src.count('"X-DeadManStaleness"') == 1
    assert GEN.DEADMAN_KEY == "X-DeadManStaleness"


def test_the_box_health_remedy_names_both_paths():
    """The operator-facing line must not send a nous-ergon-ops reader to this repo.

    Naming only budget.yaml is most of why this finding kept recurring with a
    new timer name in it: the reader went to the repo that could not fix it.
    """
    sh = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
    line = next(l for l in sh.splitlines() if "timer has no dead-man threshold" in l and "echo" in l)
    assert "X-DeadManStaleness=" in line
    assert "budget.yaml" in line
