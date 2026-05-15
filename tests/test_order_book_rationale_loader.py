"""Wiring test for load_order_book_rationale_history in s3_loader.py.

Delegates to ``alpha_engine_lib.eval_artifacts.list_eval_artifacts``;
behavioral failure-mode coverage lives in the lib. This pins the
wiring — correct bucket + prefix + n_recent, return propagated, empty
list propagated (pre-deploy graceful state). Mirrors
test_regime_substrate_loader.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))

_mock_st = MagicMock()
_mock_st.cache_data = lambda **kwargs: (lambda f: f)
_mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = _mock_st


@pytest.fixture
def loader():
    import importlib
    for mod_name in ("loaders.s3_loader", "loaders"):
        cached = sys.modules.get(mod_name)
        if cached is not None and isinstance(cached, MagicMock):
            del sys.modules[mod_name]
    import loaders.s3_loader as s3_loader
    importlib.reload(s3_loader)
    return s3_loader


_ARTIFACTS = [
    {"run_id": "2605140200", "trading_day": "2026-05-13",
     "summary": {"n_considered": 40}},
    {"run_id": "2605150200", "trading_day": "2026-05-14",
     "summary": {"n_considered": 42}},
]


def test_delegates_to_lib_with_research_bucket_and_prefix(loader):
    import alpha_engine_lib.eval_artifacts as ea
    fake_client = MagicMock()
    with patch.object(loader, "get_s3_client", return_value=fake_client):
        with patch.object(loader, "_research_bucket", return_value="alpha-engine-research"):
            with patch.object(ea, "list_eval_artifacts", return_value=_ARTIFACTS) as mock_lib:
                result = loader.load_order_book_rationale_history(n_recent=14)
    assert result == _ARTIFACTS
    mock_lib.assert_called_once_with(
        fake_client,
        bucket="alpha-engine-research",
        prefix="trades/order_book_rationale",
        n_recent=14,
    )


def test_propagates_empty_list_pre_deploy(loader):
    """Before the executor first writes the artifact the lib returns
    [] — the page must get [] and render the graceful empty notice."""
    import alpha_engine_lib.eval_artifacts as ea
    with patch.object(loader, "get_s3_client", return_value=MagicMock()):
        with patch.object(loader, "_research_bucket", return_value="b"):
            with patch.object(ea, "list_eval_artifacts", return_value=[]):
                result = loader.load_order_book_rationale_history()
    assert result == []
