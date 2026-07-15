"""
tests/test_s3_loader.py — Unit tests for loaders/s3_loader.py (private dashboard)
and live/loaders/s3_loader.py (public live console).

Tests S3 error tracking, utility functions, and the live get_s3_client()
fallback logic. No actual S3 calls — all boto3 interactions are mocked.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock streamlit before importing any loaders
mock_st = MagicMock()
# Make @st.cache_data act as a passthrough decorator
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st


# ---------------------------------------------------------------------------
# Tests: S3 error tracking (private dashboard s3_loader)
# ---------------------------------------------------------------------------

class TestS3ErrorTracking:
    """Tests for the S3 error tracking utility in the private s3_loader."""

    def test_record_and_retrieve_errors(self):
        """Errors should be recorded and retrievable."""
        # Import with config mock
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value={
                "s3": {"research_bucket": "test", "trades_bucket": "test"},
                "cache_ttl": {"signals": 900, "trades": 900, "research": 3600},
                "paths": {},
            }):
                # Force reimport
                if "loaders.s3_loader" in sys.modules:
                    del sys.modules["loaders.s3_loader"]
                from loaders import s3_loader

        # Clear any existing errors
        s3_loader._recent_s3_errors.clear()

        s3_loader._record_s3_error("test-bucket", "test/key.json", "TestError", "something broke")
        errors = s3_loader.get_recent_s3_errors()

        assert len(errors) == 1
        assert errors[0]["bucket"] == "test-bucket"
        assert errors[0]["key"] == "test/key.json"
        assert errors[0]["error_type"] == "TestError"
        assert "something broke" in errors[0]["message"]
        assert "timestamp" in errors[0]

    def test_error_cap_at_max(self):
        """Error log should be capped at _MAX_S3_ERRORS entries."""
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value={
                "s3": {"research_bucket": "test", "trades_bucket": "test"},
                "cache_ttl": {"signals": 900, "trades": 900, "research": 3600},
                "paths": {},
            }):
                if "loaders.s3_loader" in sys.modules:
                    del sys.modules["loaders.s3_loader"]
                from loaders import s3_loader

        s3_loader._recent_s3_errors.clear()
        max_errors = s3_loader._MAX_S3_ERRORS

        # Record more than max
        for i in range(max_errors + 20):
            s3_loader._record_s3_error("b", f"key_{i}", "Err", f"msg_{i}")

        errors = s3_loader.get_recent_s3_errors()
        assert len(errors) == max_errors

        # Oldest should have been dropped — first entry should be key_20
        assert errors[0]["key"] == "key_20"

    def test_message_truncated_at_200_chars(self):
        """Error messages should be truncated to 200 characters."""
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value={
                "s3": {"research_bucket": "test", "trades_bucket": "test"},
                "cache_ttl": {"signals": 900, "trades": 900, "research": 3600},
                "paths": {},
            }):
                if "loaders.s3_loader" in sys.modules:
                    del sys.modules["loaders.s3_loader"]
                from loaders import s3_loader

        s3_loader._recent_s3_errors.clear()
        long_msg = "x" * 500
        s3_loader._record_s3_error("b", "k", "Err", long_msg)

        errors = s3_loader.get_recent_s3_errors()
        assert len(errors[0]["message"]) == 200


# ---------------------------------------------------------------------------
# Tests: live console get_s3_client() fallback
# ---------------------------------------------------------------------------

class TestLiveGetS3Client:
    """Tests for live/loaders/s3_loader.py get_s3_client() fallback."""

    def _load_live_loader(self):
        """Load live/loaders/s3_loader.py via importlib with config mocked."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f"live_s3_loader_{id(self)}",
            str(Path(__file__).parent.parent / "live" / "loaders" / "s3_loader.py"),
        )
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value={
                "s3": {"trades_bucket": "test"},
                "cache_ttl": {"trades": 900},
                "paths": {"eod_pnl": "trades/eod_pnl.csv"},
            }):
                spec.loader.exec_module(module)
        return module

    def test_falls_back_to_default_client_without_secrets(self):
        """
        When st.secrets['aws'] raises KeyError, get_s3_client()
        should fall back to boto3.client('s3') (IAM role).
        """
        # Use a real empty dict so ["aws"] raises KeyError naturally.
        # Must set on sys.modules["streamlit"] directly — another test file
        # may have replaced it with a different MagicMock instance.
        st_mock = sys.modules["streamlit"]
        st_mock.secrets = {}

        with patch("boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            live_loader = self._load_live_loader()
            live_loader.get_s3_client()
            mock_boto.assert_called_with("s3")

    def test_uses_secrets_when_available(self):
        """
        When st.secrets['aws'] is available, get_s3_client() should use
        explicit credentials from secrets.
        """
        st_mock = sys.modules["streamlit"]
        st_mock.secrets = {
            "aws": {
                "AWS_ACCESS_KEY_ID": "AKIA_TEST",
                "AWS_SECRET_ACCESS_KEY": "fake",  # gitleaks: short fixture, not a real secret
                "AWS_DEFAULT_REGION": "us-west-2",
            }
        }

        live_loader = self._load_live_loader()

        with patch("boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            live_loader.get_s3_client()
            mock_boto.assert_called_with(
                "s3",
                aws_access_key_id="AKIA_TEST",
                aws_secret_access_key="fake",
                region_name="us-west-2",
            )
