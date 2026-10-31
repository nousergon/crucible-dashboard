"""Shared test fixtures for dashboard tests.

Ensures streamlit is mocked before any dashboard module imports.
Config mocking is handled per-test-file (see test_s3_loader.py pattern).

Also pins ``ALPHA_ENGINE_SECRETS_SOURCE=env`` so any future
``nousergon_lib.secrets.get_secret()`` call sites (post 2026-05-12
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
        from nousergon_lib.secrets import clear_cache
    except ImportError:
        yield
        return
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def no_live_aws(monkeypatch):
    """Make every botocore API call fail fast with ``NoCredentialsError``.

    This is what CI already sees, because CI has no AWS credentials. A test
    that renders a page whose top level calls AWS (``views/25_Pipeline_Status.py``
    pages Step Functions execution history for the reliability expander) would
    otherwise reach the network wherever credentials exist, an agent sandbox or
    a laptop: it hangs, or reads live data (alpha-engine-config-I11531). Opt in
    per module with ``pytestmark = pytest.mark.usefixtures("no_live_aws")``.
    Not autouse: it replaces ``BaseClient._make_api_call``, which botocore's
    ``Stubber`` and moto both rely on.
    """
    from botocore.client import BaseClient
    from botocore.exceptions import NoCredentialsError

    def _refuse(self, operation_name, api_params):
        raise NoCredentialsError()

    monkeypatch.setattr(BaseClient, "_make_api_call", _refuse)
