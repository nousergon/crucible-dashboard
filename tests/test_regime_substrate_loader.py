"""Tests for the regime substrate loader functions in s3_loader.py.

Since the lib v0.16.0 adoption, ``load_regime_substrate_latest`` and
``load_regime_substrate_history`` delegate to
``nousergon_lib.eval_artifacts.load_latest_eval_artifact`` and
``list_eval_artifacts`` respectively. The lib has its own comprehensive
test coverage of all failure modes (missing sidecar, malformed
artifact_key, missing body, partial-progress on listing, etc.).

These tests just verify the *wiring* — that the dashboard loaders
call the lib functions with the correct bucket + prefix arguments and
propagate their results. Behavioral coverage lives in the lib.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))


# Mock streamlit at module-import time so the @st.cache_data decorator
# in s3_loader becomes a no-op. Mirrors conftest's approach.
_mock_st = MagicMock()
_mock_st.cache_data = lambda **kwargs: (lambda f: f)
_mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = _mock_st


@pytest.fixture
def loader():
    """Force-import the real ``loaders.s3_loader`` module.

    Other test files (``test_eval_loader.py``) replace
    ``sys.modules['loaders.s3_loader']`` with a MagicMock at module-
    import time and never restore it — drop any cached MagicMock and
    reimport to get the real module.
    """
    import importlib
    for mod_name in ("loaders.s3_loader", "loaders"):
        cached = sys.modules.get(mod_name)
        if cached is not None and isinstance(cached, MagicMock):
            del sys.modules[mod_name]
    import loaders.s3_loader as s3_loader
    importlib.reload(s3_loader)
    return s3_loader


_DATED_ARTIFACT = {
    "calendar_date": "2026-05-17",
    "trading_day": "2026-05-15",
    "run_id": "2605170230",
    "schema_version": 1,
    "hmm": {"argmax": "neutral", "probs": {"bear": 0.18, "neutral": 0.62, "bull": 0.20}},
    "composite": {"intensity_z": 0.15},
    "bocpd": {"change_signal": False},
}


class TestLoadRegimeSubstrateLatest:
    """The dashboard loader delegates to nousergon_lib's
    load_latest_eval_artifact. These tests pin the wiring (correct
    bucket + prefix, propagated return value) — failure-mode behavior
    is covered by the lib's own tests."""

    def test_delegates_to_lib_with_research_bucket_and_regime_prefix(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=_DATED_ARTIFACT) as mock_lib:
                    result = loader.load_regime_substrate_latest()
        assert result == _DATED_ARTIFACT
        mock_lib.assert_called_once_with(
            fake_client, bucket="alpha-engine-research", prefix="regime",
        )

    def test_propagates_none_from_lib(self, loader):
        """When the lib returns None (substrate not yet published), the
        dashboard loader must propagate None — the Regime page renders
        a graceful 'no substrate yet' warning."""
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=None):
                    result = loader.load_regime_substrate_latest()
        assert result is None


class TestLoadRegimeSubstrateHistory:
    """Wiring tests for the history loader — delegates to lib's
    list_eval_artifacts with the n_recent cap."""

    def test_delegates_to_lib_with_n_recent_capped(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        sentinel = [
            {"run_id": "2604120230"},
            {"run_id": "2604260230"},
            {"run_id": "2605170230"},
        ]
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "list_eval_artifacts", return_value=sentinel) as mock_lib:
                    result = loader.load_regime_substrate_history(n_weeks=10)
        assert result == sentinel
        mock_lib.assert_called_once_with(
            fake_client,
            bucket="alpha-engine-research",
            prefix="regime",
            n_recent=10,
        )

    def test_default_n_weeks_is_26(self, loader):
        """26-week default matches the dashboard's history-window
        display range — pin to catch accidental drift."""
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "list_eval_artifacts", return_value=[]) as mock_lib:
                    loader.load_regime_substrate_history()
        kwargs = mock_lib.call_args.kwargs
        assert kwargs.get("n_recent") == 26

    def test_returns_empty_when_lib_returns_empty(self, loader):
        """Pre-deploy state — no substrate artifacts yet → empty list."""
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "list_eval_artifacts", return_value=[]):
                    result = loader.load_regime_substrate_history(n_weeks=26)
        assert result == []


class TestLoadFastSignalLatest:
    """Stage F2 daily fast-signal loader — same lib-delegation wiring,
    distinct ``regime/fast_signal`` prefix + cadence."""

    _FAST = {
        "trading_day": "2026-05-15", "run_id": "2605150615",
        "forced_bear": True, "warmup": False, "change_confidence": 0.71,
        "intensity_z": -1.8,
    }

    def test_delegates_with_fast_signal_prefix(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=self._FAST) as mock_lib:
                    result = loader.load_fast_signal_latest()
        assert result == self._FAST
        mock_lib.assert_called_once_with(
            fake_client, bucket="alpha-engine-research", prefix="regime/fast_signal",
        )

    def test_propagates_none_from_lib(self, loader):
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=None):
                    result = loader.load_fast_signal_latest()
        assert result is None


class TestLoadDrawdownLeg:
    """3rd ensemble leg daily loaders — same lib-delegation wiring,
    distinct ``regime/drawdown`` prefix."""

    _DD = {
        "trading_day": "2026-05-19", "run_id": "2605190615",
        "spy": {"tier": "caution", "drawdown": -0.072, "peak": 600.0},
        "excess": {"available": False, "tier": "risk_on"},
        "effective_regime": {"effective_regime": "caution",
                             "drivers": {"drawdown_spy": "caution"}},
        "observed": True, "cold_start": False,
    }

    def test_latest_delegates_with_drawdown_prefix(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=self._DD) as mock_lib:
                    result = loader.load_drawdown_leg_latest()
        assert result == self._DD
        mock_lib.assert_called_once_with(
            fake_client, bucket="alpha-engine-research", prefix="regime/drawdown",
        )

    def test_latest_propagates_none_from_lib(self, loader):
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=None):
                    result = loader.load_drawdown_leg_latest()
        assert result is None

    def test_history_delegates_with_n_recent_capped(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        sentinel = [self._DD, self._DD]
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "list_eval_artifacts", return_value=sentinel) as mock_lib:
                    result = loader.load_drawdown_leg_history(n_days=7)
        assert result == sentinel
        mock_lib.assert_called_once_with(
            fake_client, bucket="alpha-engine-research",
            prefix="regime/drawdown", n_recent=7,
        )

    def test_history_default_n_days_is_14(self, loader):
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "list_eval_artifacts", return_value=[]) as mock_lib:
                    loader.load_drawdown_leg_history()
        assert mock_lib.call_args.kwargs["n_recent"] == 14
