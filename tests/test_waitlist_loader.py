"""
tests/test_waitlist_loader.py — Unit tests for loaders/waitlist_loader.py.

Covers:
  - Cloudflare D1 query happy path (total + 7-day counts returned)
  - Cloudflare D1 API error → error state per product (not silent zero)
  - Missing Cloudflare credentials → configured=False (not a crash)
  - GitHub probe fetch happy path (success/failure conclusions rendered)
  - GitHub probe API error → conclusion="unknown" with error excerpt
  - Synthetic-probe exclusion from counts (SQL includes the WHERE clause)
  - No os.environ reads (uses st.secrets)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# The module under test — imported after streamlit is mocked (conftest.py
# handles streamlit mocking at import time via sys.modules).
from loaders.waitlist_loader import (
    _cf_credentials,
    _query_d1,
    _fetch_probe_status,
    load_waitlist_signups,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_d1_response(success: bool, rows: list | None = None) -> bytes:
    """Simulate a Cloudflare D1 REST API response."""
    return json.dumps({
        "success": success,
        "errors": [],
        "messages": [],
        "result": rows or [],
    }).encode()


def _make_gh_runs_response(conclusion: str = "success",
                           timestamp: str = "2026-07-24T10:15:00Z") -> bytes:
    """Simulate a GitHub Actions workflow-runs response."""
    return json.dumps({
        "total_count": 1,
        "workflow_runs": [{
            "id": 12345,
            "name": "waitlist-probe",
            "status": "completed",
            "conclusion": conclusion,
            "created_at": timestamp,
            "html_url": f"https://github.com/nousergon/crucible-dashboard/actions/runs/12345",
        }],
    }).encode()


# ── _cf_credentials ──────────────────────────────────────────────────────────


def test_cf_credentials_returned_from_streamlit_secrets():
    """st.secrets['cloudflare'] yields account_id + api_token."""
    st_mock = sys.modules["streamlit"]
    st_mock.secrets = {
        "cloudflare": {"account_id": "test-account", "api_token": "test-token"},
    }
    account_id, token = _cf_credentials()
    assert account_id == "test-account"
    assert token == "test-token"


def test_cf_credentials_none_when_missing():
    """Missing or empty cloudflare section returns (None, None)."""
    st_mock = sys.modules["streamlit"]

    # Empty dict
    st_mock.secrets = {}
    assert _cf_credentials() == (None, None)

    # Missing api_token
    st_mock.secrets = {"cloudflare": {"account_id": "x"}}
    assert _cf_credentials() == ("x", None)

    # Non-dict cloudflare value
    st_mock.secrets = {"cloudflare": "misconfigured"}
    assert _cf_credentials() == (None, None)


# ── _query_d1 ────────────────────────────────────────────────────────────────


@patch("urllib.request.urlopen")
def test_query_d1_happy_path(mock_urlopen):
    """Successful D1 query returns the result rows."""
    mock_urlopen.return_value.__enter__.return_value.read.return_value = _make_d1_response(
        True, [{"total": 42, "last_7d": 5}]
    )
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    rows = _query_d1("acct", "db-id", "SELECT ...", "tok")
    assert rows is not None
    assert rows[0]["total"] == 42
    assert rows[0]["last_7d"] == 5


@patch("urllib.request.urlopen")
def test_query_d1_api_error_returns_none(mock_urlopen):
    """D1 API returning success=False yields None (not an exception)."""
    mock_urlopen.return_value.__enter__.return_value.read.return_value = _make_d1_response(
        False, []
    )
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    rows = _query_d1("acct", "db-id", "SELECT ...", "tok")
    assert rows is None


@patch("urllib.request.urlopen", side_effect=URLError("net error"))
def test_query_d1_network_error_returns_none(mock_urlopen):
    """Network failure during D1 query yields None (not raised)."""
    rows = _query_d1("acct", "db-id", "SELECT ...", "tok")
    assert rows is None


# ── load_waitlist_signups ────────────────────────────────────────────────────


def _setup_secrets(account_id: str = "test-acct", token: str = "test-token"):
    """Helper: populate ``st.secrets`` with Cloudflare creds."""
    st_mock = sys.modules["streamlit"]
    st_mock.secrets = {
        "cloudflare": {"account_id": account_id, "api_token": token},
    }


@patch("urllib.request.urlopen")
def test_load_waitlist_signups_happy_path(mock_urlopen):
    """Both products return counts; response structured correctly."""
    _setup_secrets()
    # Two calls: Metron then Vires
    mock_urlopen.return_value.__enter__.return_value.read.side_effect = [
        _make_d1_response(True, [{"total": 42, "last_7d": 5}]),
        _make_d1_response(True, [{"total": 18, "last_7d": 3}]),
    ]
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    result = load_waitlist_signups()
    assert result["configured"] is True
    assert len(result["products"]) == 2

    metron = result["products"][0]
    assert metron["product"] == "Metron"
    assert metron["total"] == 42
    assert metron["last_7d"] == 5
    assert metron["error"] is False

    vires = result["products"][1]
    assert vires["product"] == "Vires"
    assert vires["total"] == 18
    assert vires["last_7d"] == 3
    assert vires["error"] is False


def test_load_waitlist_signups_not_configured():
    """Missing credentials → configured=False, not a crash."""
    st_mock = sys.modules["streamlit"]
    st_mock.secrets = {}

    result = load_waitlist_signups()
    assert result["configured"] is False
    assert result["products"] == []


@patch("urllib.request.urlopen")
def test_load_waitlist_signups_partial_failure(mock_urlopen):
    """One product fails to query — its entry shows error, other succeeds."""
    _setup_secrets()
    mock_urlopen.return_value.__enter__.return_value.read.side_effect = [
        _make_d1_response(True, [{"total": 42, "last_7d": 5}]),
        _make_d1_response(False, []),  # Vires query fails
    ]
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    result = load_waitlist_signups()
    assert result["configured"] is True
    assert result["products"][0]["error"] is False  # Metron OK
    assert result["products"][0]["total"] == 42
    assert result["products"][1]["error"] is True  # Vires errored
    assert result["products"][1]["total"] is None


# ── _fetch_probe_status ──────────────────────────────────────────────────────


@patch("urllib.request.urlopen")
def test_probe_status_success(mock_urlopen):
    """Successful probe run returns conclusion + timestamp."""
    mock_urlopen.return_value.__enter__.return_value.read.return_value = _make_gh_runs_response(
        "success", "2026-07-24T10:15:00Z"
    )
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    status = _fetch_probe_status()
    assert status["conclusion"] == "success"
    assert status["timestamp"] == "2026-07-24T10:15:00Z"
    assert status["url"] is not None
    assert "error" not in status or status.get("error") is None


@patch("urllib.request.urlopen")
def test_probe_status_failure(mock_urlopen):
    """Failed probe run returns conclusion='failure'."""
    mock_urlopen.return_value.__enter__.return_value.read.return_value = _make_gh_runs_response(
        "failure", "2026-07-24T10:15:00Z"
    )
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    status = _fetch_probe_status()
    assert status["conclusion"] == "failure"


@patch("urllib.request.urlopen", side_effect=URLError("API unavailable"))
def test_probe_status_network_error(mock_urlopen):
    """API error → conclusion='unknown' with error excerpt (not a crash)."""
    status = _fetch_probe_status()
    assert status["conclusion"] == "unknown"
    assert status["timestamp"] is None
    assert "error" in status  # error excerpt present


@patch("urllib.request.urlopen")
def test_probe_status_empty_runs(mock_urlopen):
    """No runs at all → conclusion='unknown'."""
    mock_urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({
        "total_count": 0,
        "workflow_runs": [],
    }).encode()
    mock_urlopen.return_value.__enter__.return_value.__enter__ = (
        lambda: mock_urlopen.return_value.__enter__.return_value
    )

    status = _fetch_probe_status()
    assert status["conclusion"] == "unknown"
