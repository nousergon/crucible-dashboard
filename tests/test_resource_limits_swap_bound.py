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


def _parse_mb(value: str) -> int:
    m = re.fullmatch(r"(\d+)([MG])", value)
    assert m, f"unparseable size {value!r}"
    return int(m.group(1)) * (1024 if m.group(2) == "G" else 1)


def _services() -> dict[str, dict]:
    spec = yaml.safe_load(BUDGET.read_text())
    return {s["unit"]: s for s in spec["services"]}


def test_dashboard_service_declares_a_swap_bound():
    svc = _services()["dashboard.service"]
    assert "memory_swap_max" in svc, "dashboard.service must bound its swap (I7445)"
    swap = _parse_mb(svc["memory_swap_max"])
    # A swap bound of zero would turn every memory_high reclaim into an OOM;
    # one at or above the RAM cap would not bound the runaway that was measured.
    assert 0 < swap < _parse_mb(svc["memory_max"])


def test_dry_run_renders_swap_bound_only_where_declared(tmp_path):
    env = dict(os.environ)
    env["RUNTIME_DROPIN_ROOT"] = str(tmp_path)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    out = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run"],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    declared = {u for u, s in _services().items() if s.get("memory_swap_max")}
    assert declared, "no unit declares memory_swap_max"
    for line in out.splitlines():
        m = re.search(r"/etc/systemd/system/([^/]+)\.d/", line)
        if not m or not line.startswith("would write"):
            continue
        unit = m.group(1)
        if unit in declared:
            want = _services()[unit]["memory_swap_max"]
            assert f"MemorySwapMax={want}" in line, line
        else:
            assert "MemorySwapMax" not in line, line


def test_swap_line_rides_the_memorymax_line():
    # A unit that declares no swap bound must render byte-identically to
    # before: the optional directive is appended to the MemoryMax line, never
    # given a line of its own that would leave a blank in every other drop-in.
    sh = INSTALLER.read_text()
    assert "MemoryMax=${max}${swap_line}" in sh
    assert "{s.get('memory_swap_max', '')}" in sh
