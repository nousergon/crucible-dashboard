"""Tests for the T1 + T2 regime eval loaders in s3_loader.py.

Mirrors the structure of ``test_regime_substrate_loader.py`` — wiring
tests that pin the dashboard loaders delegate to the lib helpers with
the correct bucket + prefix arguments. Behavioral coverage of
``load_latest_eval_artifact`` + ``list_eval_artifacts`` lives in the
alpha-engine-lib's own tests.

Two loaders × {latest, history} = 4 callsites, each with the same
delegation contract:

  - load_regime_retrospective_eval_latest    → regime/retrospective
  - load_regime_retrospective_eval_history   → regime/retrospective
  - load_regime_stratified_sortino_latest    → regime/stratified_sortino
  - load_regime_stratified_sortino_history   → regime/stratified_sortino
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))


# Mock streamlit so @st.cache_data is a no-op at import time. Mirrors
# test_regime_substrate_loader.py's approach.
_mock_st = MagicMock()
_mock_st.cache_data = lambda **kwargs: (lambda f: f)
_mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = _mock_st


@pytest.fixture
def loader():
    """Force-reimport loaders.s3_loader to drop any MagicMock cache
    from sibling test modules."""
    import importlib
    for mod_name in ("loaders.s3_loader", "loaders"):
        cached = sys.modules.get(mod_name)
        if cached is not None and isinstance(cached, MagicMock):
            del sys.modules[mod_name]
    import loaders.s3_loader as s3_loader
    importlib.reload(s3_loader)
    return s3_loader


# ─────────────────────────────────────────────────────────────────────
# T1 — retrospective HMM smoothing eval loaders
# ─────────────────────────────────────────────────────────────────────


_T1_ARTIFACT = {
    "calendar_date": "2026-05-17",
    "trading_day": "2026-05-15",
    "run_id": "2605170230",
    "schema_version": 1,
    "eval_tier": "T1_retrospective_hmm_smoothing",
    "lag_weeks": 8,
    "score": {
        "n_pairings": 25,
        "asymmetric_weighted_agreement_rate": 0.78,
        "rolling_window_score": 0.82,
    },
}


class TestLoadRegimeRetrospectiveEvalLatest:
    def test_delegates_with_research_bucket_and_retrospective_prefix(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=_T1_ARTIFACT) as mock_lib:
                    result = loader.load_regime_retrospective_eval_latest()
        assert result == _T1_ARTIFACT
        mock_lib.assert_called_once_with(
            fake_client,
            bucket="alpha-engine-research",
            prefix="regime/retrospective",
        )

    def test_propagates_none_when_lib_returns_none(self, loader):
        """Cold-start (~8 weeks after Lambda creation) → artifact may be
        absent → loader returns None; page renders 'no T1 yet' warning."""
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=None):
                    result = loader.load_regime_retrospective_eval_latest()
        assert result is None


class TestLoadRegimeRetrospectiveEvalHistory:
    def test_delegates_with_n_recent(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        sentinel = [_T1_ARTIFACT, {**_T1_ARTIFACT, "run_id": "2604260230"}]
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "list_eval_artifacts", return_value=sentinel) as mock_lib:
                    result = loader.load_regime_retrospective_eval_history(n_weeks=12)
        assert result == sentinel
        mock_lib.assert_called_once_with(
            fake_client,
            bucket="alpha-engine-research",
            prefix="regime/retrospective",
            n_recent=12,
        )

    def test_default_n_weeks_is_26(self, loader):
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "list_eval_artifacts", return_value=[]) as mock_lib:
                    loader.load_regime_retrospective_eval_history()
        kwargs = mock_lib.call_args.kwargs
        assert kwargs.get("n_recent") == 26


# ─────────────────────────────────────────────────────────────────────
# T2 — downstream-stratified Sortino eval loaders
# ─────────────────────────────────────────────────────────────────────


_T2_ARTIFACT = {
    "calendar_date": "2026-05-17",
    "trading_day": "2026-05-15",
    "run_id": "2605170230",
    "schema_version": 1,
    "eval_tier": "T2_downstream_stratified_sortino",
    "spread_10d": {
        "horizon_days": 10,
        "spread_bull_minus_bear_sortino": 0.42,
        "interpretation": "regime_signal_useful",
    },
    "spread_30d": {
        "horizon_days": 30,
        "spread_bull_minus_bear_sortino": 0.21,
        "interpretation": "regime_signal_neutral",
    },
    "strata": [],
}


class TestLoadRegimeStratifiedSortinoLatest:
    def test_delegates_with_stratified_sortino_prefix(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=_T2_ARTIFACT) as mock_lib:
                    result = loader.load_regime_stratified_sortino_latest()
        assert result == _T2_ARTIFACT
        mock_lib.assert_called_once_with(
            fake_client,
            bucket="alpha-engine-research",
            prefix="regime/stratified_sortino",
        )

    def test_propagates_none_when_lib_returns_none(self, loader):
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "load_latest_eval_artifact", return_value=None):
                    result = loader.load_regime_stratified_sortino_latest()
        assert result is None


class TestLoadRegimeStratifiedSortinoHistory:
    def test_delegates_with_n_recent(self, loader):
        import nousergon_lib.eval_artifacts as ea
        fake_client = MagicMock()
        sentinel = [_T2_ARTIFACT, {**_T2_ARTIFACT, "run_id": "2604260230"}]
        with patch.object(loader, "get_s3_client", return_value=fake_client):
            with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
                with patch.object(ea, "list_eval_artifacts", return_value=sentinel) as mock_lib:
                    result = loader.load_regime_stratified_sortino_history(n_weeks=12)
        assert result == sentinel
        mock_lib.assert_called_once_with(
            fake_client,
            bucket="alpha-engine-research",
            prefix="regime/stratified_sortino",
            n_recent=12,
        )

    def test_default_n_weeks_is_26(self, loader):
        import nousergon_lib.eval_artifacts as ea
        with patch.object(loader, "get_s3_client", return_value=MagicMock()):
            with patch.object(loader, "_research_bucket", return_value="b"):
                with patch.object(ea, "list_eval_artifacts", return_value=[]) as mock_lib:
                    loader.load_regime_stratified_sortino_history()
        kwargs = mock_lib.call_args.kwargs
        assert kwargs.get("n_recent") == 26


# ─────────────────────────────────────────────────────────────────────
# Page-level pin — Regime page imports both T1 + T2 loaders
# ─────────────────────────────────────────────────────────────────────


def test_regime_page_imports_eval_loaders():
    """The Regime page must import the four new eval loaders. Catches
    an accidental removal of the T1/T2 dashboard tabs during refactors."""
    page = Path(__file__).resolve().parents[1] / "views" / "15_Regime.py"
    body = page.read_text()
    assert "load_regime_retrospective_eval_latest" in body
    assert "load_regime_retrospective_eval_history" in body
    assert "load_regime_stratified_sortino_latest" in body
    assert "load_regime_stratified_sortino_history" in body
