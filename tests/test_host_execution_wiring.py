"""Nav-wiring guard for the unified Execution front page (host_execution).

The console IA files the executor-stage surfaces — Order Book Rationale,
Execution, Optimizer Decision, Optimizer Risk — under ONE tabbed front page
(``views/host_execution.py``). The optimizer is the executor's planning stage,
so these belong here, not scattered under Research & Signals (where Order Book
had drifted) or Backtester & Eval (where Optimizer Risk/Decision had drifted).

This guard pins that wiring so a future edit can't silently:
  * point a tab at a non-existent view file,
  * collide host keys,
  * leave Order Book / Optimizer tabs duplicated in their old hosts,
  * drop the host from app.py's navigation.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VIEWS = REPO_ROOT / "views"

# (label, filename) tabs expected on the Execution front page, in order.
# Optimizer Decision + Risk merged into one Optimizer tab (two lenses on the
# same optimizer_shadow artifact) — console-IA phase 1, config#1990.
EXPECTED_EXECUTION_TABS = [
    ("Order Book", "16_Order_Book_Rationale.py"),
    ("Execution", "6_Execution.py"),
    ("Optimizer", "Optimizer.py"),
]


def _host_tabs(filename: str) -> list[tuple[str, str]]:
    """Extract the ``(label, "NN_File.py")`` tuples a host registers."""
    src = (VIEWS / filename).read_text()
    return [(m.group(1), m.group(2))
            for m in re.finditer(r'\(\s*"([^"]+)"\s*,\s*"([^"]+\.py)"\s*\)', src)]


def _host_key(filename: str) -> str | None:
    src = (VIEWS / filename).read_text()
    m = re.search(r'key\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else None


def test_host_execution_exists_with_expected_tabs():
    assert (VIEWS / "host_execution.py").exists()
    assert _host_tabs("host_execution.py") == EXPECTED_EXECUTION_TABS


def test_every_execution_tab_file_exists():
    for _label, filename in _host_tabs("host_execution.py"):
        assert (VIEWS / filename).exists(), f"tab points at missing view {filename}"


def test_host_execution_key_is_unique():
    keys = [
        _host_key(p.name)
        for p in VIEWS.glob("host_*.py")
    ]
    keys = [k for k in keys if k]
    assert _host_key("host_execution.py") == "host_execution"
    assert len(keys) == len(set(keys)), f"duplicate host keys: {keys}"


def test_app_registers_host_execution_not_standalone_execution():
    app = (REPO_ROOT / "app.py").read_text()
    assert 'page("host_execution.py"' in app
    # 6_Execution is now a TAB inside the host — it must not also be a
    # standalone nav entry.
    assert 'page("6_Execution.py"' not in app


def test_order_book_removed_from_research_host():
    tabs = _host_tabs("host_research_signals.py")
    files = [f for _, f in tabs]
    assert "16_Order_Book_Rationale.py" not in files


def test_optimizer_tabs_removed_from_eval_host():
    # host_eval_backtester.py (the eval host the optimizer surfaces had
    # already left) was itself collapsed — it wrapped exactly one sub-view,
    # pure UI chrome (config#2557) — so 8_Eval_Quality.py is now registered
    # directly. Guard that the optimizer tabs never drifted back in, and
    # that the retired host wrapper + the older stale host_eval_optimizer.py
    # both stay gone.
    assert not (VIEWS / "host_eval_backtester.py").exists()
    assert not (VIEWS / "host_eval_optimizer.py").exists()
    eval_quality_src = (VIEWS / "8_Eval_Quality.py").read_text()
    assert "30_Optimizer_Risk.py" not in eval_quality_src
    assert "32_Optimizer_Decision.py" not in eval_quality_src
    app = (REPO_ROOT / "app.py").read_text()
    assert 'page("host_eval_backtester.py"' not in app
    assert 'views/8_Eval_Quality.py"' in app
    assert "host_eval_optimizer.py" not in app
