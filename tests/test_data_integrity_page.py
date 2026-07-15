"""Data Integrity page contracts: nav registration + render smoke.

Mirrors ``tests/test_fleet_status_page.py``'s pattern: exec-load the page
with streamlit mocked and the loader's signal-gathering stubbed (no S3/
network), and assert it renders without raising for both a clean (all
green) and a synthetic-disagreement (amber/red) input set — this is the
config#2458 closes-when criterion ("verify by forcing a synthetic
quarantine/divergence... confirming the tile goes amber/red") exercised at
the page level, on top of the pure-logic coverage in
``tests/test_data_integrity_status.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

PAGE = REPO_ROOT / "views" / "50_Data_Integrity.py"


class TestNavRegistration:
    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_app_registers_data_integrity_page(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert '"50_Data_Integrity.py"' in app_src


def _render(signals):
    """Exec-load the page with gather_data_integrity_signals stubbed to
    return *signals*; return (mock streamlit module, shared cols list) for
    assertions. ``cols`` is the SAME list of MagicMocks returned by every
    ``st.columns(...)`` call the page makes, so ``cols[0]`` is the "Overall"
    metric column regardless of how many columns a given call requested."""
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    cols = [MagicMock() for _ in range(6)]
    mock_st.columns = MagicMock(
        side_effect=lambda spec: cols[: len(spec)] if isinstance(spec, (list, tuple)) else cols[: int(spec)]
    )

    import loaders.data_integrity_loader as dil

    saved_st = sys.modules.get("streamlit")
    sys.modules["streamlit"] = mock_st
    saved_gather = dil.gather_data_integrity_signals
    dil.gather_data_integrity_signals = lambda: signals
    try:
        spec = importlib.util.spec_from_file_location("data_integrity_page", PAGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        dil.gather_data_integrity_signals = saved_gather
        if saved_st is not None:
            sys.modules["streamlit"] = saved_st
    return mock_st, cols


class TestRenderSmokeClean:
    def test_renders_without_raising_when_clean(self):
        from data_integrity_status import GREEN, GateSignal

        sig = GateSignal(
            phase="L1", label="Cross-source agreement (settled closes)",
            dot=GREEN, flagged_count=0, total_count=5, detail=(),
        )
        mock_st, _cols = _render([sig])
        mock_st.title.assert_called_once()

    def test_overall_metric_is_green_when_all_clean(self):
        from data_integrity_status import GREEN, GateSignal

        sig = GateSignal(phase="L1", label="x", dot=GREEN, total_count=5)
        _mock_st, cols = _render([sig])
        overall_calls = cols[0].metric.call_args_list
        assert any("GREEN" in str(c) for c in overall_calls)


class TestRenderSmokeFlagged:
    def test_renders_without_raising_when_synthetic_quarantine_injected(self):
        from data_integrity_status import RED, GateSignal

        sig = GateSignal(
            phase="L1", label="Cross-source agreement (settled closes)",
            dot=RED, flagged_count=1, total_count=5,
            detail=({
                "ticker": "SPY", "xsource_status": "quarantined",
                "xsource_flagged": True, "xsource_agreement_bps": 45.0,
                "xsource_provenance": "SPY@2026-07-13: polygon=734.30 yfinance=736.60 DISAGREE@31.30bps QUARANTINED",
            },),
        )
        mock_st, _cols = _render([sig])
        mock_st.title.assert_called_once()
        mock_st.dataframe.assert_called()

    def test_overall_metric_is_red_when_quarantine_present(self):
        from data_integrity_status import RED, GateSignal

        sig = GateSignal(phase="L1", label="x", dot=RED, flagged_count=1, total_count=5, detail=({"ticker": "SPY"},))
        _mock_st, cols = _render([sig])
        overall_calls = cols[0].metric.call_args_list
        assert any("RED" in str(c) for c in overall_calls)
