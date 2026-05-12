"""Shared test fixtures for dashboard tests.

Ensures streamlit is mocked before any dashboard module imports.
Config mocking is handled per-test-file (see test_s3_loader.py pattern).

Also pins ``ALPHA_ENGINE_SECRETS_SOURCE=env`` so any future
``alpha_engine_lib.secrets.get_secret()`` call sites (post 2026-05-12
.env→SSM migration, PR 7 of the arc) read from monkeypatched env vars
only. Dashboard has zero secret reads today (preventive setup), but
this keeps the regression-test gate honest if future code adds one.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ALPHA_ENGINE_SECRETS_SOURCE", "env")

# Mock streamlit before any dashboard module imports
if "streamlit" not in sys.modules:
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kwargs: (lambda f: f)
    mock_st.cache_resource = lambda **kwargs: (lambda f: f)
    sys.modules["streamlit"] = mock_st


@pytest.fixture(autouse=True)
def _isolate_secrets_from_ssm(monkeypatch):
    """Re-pin ``ALPHA_ENGINE_SECRETS_SOURCE=env`` per test + clear the
    per-process secret cache. See
    ``alpha-engine-docs/private/env-to-ssm-260512.md`` § Risks.
    """
    monkeypatch.setenv("ALPHA_ENGINE_SECRETS_SOURCE", "env")
    try:
        from alpha_engine_lib.secrets import clear_cache
    except ImportError:
        yield
        return
    clear_cache()
    yield
    clear_cache()
