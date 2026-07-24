"""Fleet SLA page contracts: pinned deep-link slug + render smoke
(config#2858). Mirrors ``tests/test_fleet_status_page.py``.

1. **Slug contract.** ``app.py`` MUST register ``views/54_Fleet_SLA.py``
   as a standalone ``st.Page`` with ``url_path="fleet-sla"``.
2. **Render smoke.** The page exec-loads with streamlit mocked and
   ``gather_sla_inputs`` stubbed (no AWS / network) and renders the table
   without raising.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

EXPECTED_SLUG = "fleet-sla"
PAGE = REPO_ROOT / "views" / "54_Fleet_SLA.py"


class TestSlugContract:
    def test_app_pins_fleet_sla_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_pinned_page_is_the_fleet_sla_view(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        idx = app_src.rindex("views/54_Fleet_SLA.py")
        window = app_src[idx : idx + 300]
        assert f'url_path="{EXPECTED_SLUG}"' in window

    def test_page_file_exists(self):
        assert PAGE.exists()


def _healthy_inputs():
    from sla_status import SlaInputs, SlaRegistryRow

    now = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
    registry = (
        SlaRegistryRow("weekly_a", "saturday_sf", 60, "nousergon-data", "critical"),
        SlaRegistryRow("preopen_a", "weekday_sf", 60, "crucible-research", "warning"),
        SlaRegistryRow("continuous_a", "continuous", 0, "crucible-executor", "warning"),
    )
    check_results = {
        "results": [
            {"artifact_id": "weekly_a", "state": "fresh",
             "last_modified": "2026-07-04T09:30:00Z", "reason": ""},
            {"artifact_id": "preopen_a", "state": "missing", "reason": "absent"},
            {"artifact_id": "continuous_a", "state": "fresh",
             "last_modified": "2026-07-07T14:55:00Z", "reason": ""},
        ]
    }
    history = {
        "artifacts": {
            "weekly_a": {"gap_count": 0, "lookback_cycles": 12, "is_latest_pointer": False},
            "preopen_a": {"gap_count": 2, "lookback_cycles": 30, "is_latest_pointer": False},
        }
    }
    return SlaInputs(now=now, registry=registry, check_results=check_results, history=history)


@pytest.fixture
def rendered_page():
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    cols = [MagicMock() for _ in range(6)]
    mock_st.columns = MagicMock(
        side_effect=lambda spec: cols[: len(spec)] if isinstance(spec, (list, tuple)) else cols[: int(spec)]
    )
    mock_st.multiselect.side_effect = lambda label, options, default=None, **kw: default

    import loaders.sla_status_loader as ssl

    saved_st = sys.modules.get("streamlit")
    sys.modules["streamlit"] = mock_st
    saved_gather = ssl.gather_sla_inputs
    ssl.gather_sla_inputs = _healthy_inputs
    try:
        spec = importlib.util.spec_from_file_location("fleet_sla_page", PAGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        ssl.gather_sla_inputs = saved_gather
        if saved_st is not None:
            sys.modules["streamlit"] = saved_st
    return mock_st


class TestRenderSmoke:
    def test_page_renders_without_raising(self, rendered_page):
        rendered_page.title.assert_called_once()

    def test_no_warning_banner_when_healthy(self, rendered_page):
        rendered_page.warning.assert_not_called()

    def test_renders_subheader_with_table(self, rendered_page):
        subheaders = [c.args[0] for c in rendered_page.subheader.call_args_list]
        assert any("Process SLA table" in s for s in subheaders)

    def test_dataframe_rendered(self, rendered_page):
        rendered_page.dataframe.assert_called_once()
