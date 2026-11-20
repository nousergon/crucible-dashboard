"""`memory_swap_max` in budget.yaml renders MemorySwapMax= (alpha-engine-config-I7445).

MemoryMax bounds a unit's RAM charge only. Without MemorySwapMax a runaway
parks everything above MemoryMax in the box's swap, which every other unit's
memory_high reclaim depends on. Measured 2026-10-28: dashboard.service held
938 MiB anon+swap against a 450M MemoryMax and box swap peaked at 98.6%.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
INSTALLER = REPO_ROOT / "infrastructure" / "install-resource-limits.sh"
BUDGET = REPO_ROOT / "infrastructure" / "systemd" / "resource-limits" / "budget.yaml"


def test_dry_run_renders_swap_bound_only_where_declared(tmp_path):
    spec = yaml.safe_load(BUDGET.read_text())
    spec["services"] = [
        {"unit": "bounded.service", "memory_high": "80M", "memory_max": "100M",
         "memory_swap_max": "25M"},
        {"unit": "unbounded.service", "memory_high": "80M", "memory_max": "100M"},
    ]
    fixture_budget = tmp_path / "budget.yaml"
    fixture_budget.write_text(yaml.safe_dump(spec))
    child_vars = dict(os.environ, BUDGET=str(fixture_budget), RUNTIME_DROPIN_ROOT=str(tmp_path))
    child_vars.pop("TELEGRAM_BOT_TOKEN", None)
    child_vars.pop("TELEGRAM_CHAT_ID", None)
    out = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run"],
        capture_output=True, text=True, env=child_vars, check=True,
    ).stdout
    rendered = {}
    for line in out.splitlines():
        match = re.match(r"would write /etc/systemd/system/([^/]+)\.d/", line)
        if match:
            rendered[match.group(1)] = line
    assert set(rendered) == {"bounded.service", "unbounded.service"}
    assert "MemorySwapMax=25M" in rendered["bounded.service"]
    assert "MemorySwapMax" not in rendered["unbounded.service"]


def test_swap_line_rides_the_memorymax_line():
    # A unit that declares no swap bound must render byte-identically to
    # before: the optional directive is appended to the MemoryMax line, never
    # given a line of its own that would leave a blank in every other drop-in.
    sh = INSTALLER.read_text()
    assert "MemoryMax=${max}${swap_line}" in sh
    assert "{s.get('memory_swap_max', '')}" in sh
