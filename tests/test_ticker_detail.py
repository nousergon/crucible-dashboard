"""Tests for the per-ticker detail modal (ROADMAP L176).

Covers:
  - live/loaders/s3_loader.py additions: load_universe_archive (key
    construction + None-on-empty-ticker) + load_order_book_rationale (key).
  - live/ticker_detail.py pure helpers: _fmt_pct, _position_info (dict /
    list / missing), _signals_entry, _obr_block.

Uses the importlib-from-file pattern (mirrors test_s3_loader.py) so the
live modules load without polluting sys.path / sys.modules for the rest
of the suite. streamlit is mocked globally by conftest.py.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Preloaded so they're in sys.modules BEFORE _load_live_loader's
# `patch("builtins.open", ...)` window — otherwise a first-time `import
# pandas` during exec_module reads files through the mocked open (on macOS
# it reads a SystemVersion plist → re TypeError). With them preloaded the
# open-mock only covers the live module's config.yaml read.
import pandas  # noqa: F401
import yaml  # noqa: F401
import pytest

_LIVE = Path(__file__).parent.parent / "live"


def _load_live_loader():
    """Load live/loaders/s3_loader.py via importlib with config mocked.

    The @st.cache_data(ttl=_ttl(...)) decorators evaluate _ttl() → load_config()
    at module-exec time, which reads the gitignored live/config.yaml (absent in
    CI). Mock open + yaml.safe_load during exec_module, mirroring
    test_s3_loader.py::TestLiveGetS3Client._load_live_loader."""
    spec = importlib.util.spec_from_file_location(
        f"live_s3_loader_td_{id(object())}", str(_LIVE / "loaders" / "s3_loader.py")
    )
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.open", MagicMock()):
        with patch("yaml.safe_load", return_value={
            "s3": {"research_bucket": "test", "trades_bucket": "test"},
            "cache_ttl": {"research": 3600, "trades": 900},
            "paths": {"eod_pnl": "trades/eod_pnl.csv"},
        }):
            spec.loader.exec_module(module)
    return module


def _load_ticker_detail(stub_loader):
    """Load live/ticker_detail.py with its `from loaders.s3_loader import ...`
    satisfied by a stub module, fully isolated from the real loaders + the
    top-level package. Restores sys.modules after import."""
    saved = {k: sys.modules.get(k) for k in ("loaders", "loaders.s3_loader")}
    pkg = type(sys)("loaders")
    pkg.s3_loader = stub_loader
    sys.modules["loaders"] = pkg
    sys.modules["loaders.s3_loader"] = stub_loader
    try:
        spec = importlib.util.spec_from_file_location(
            f"live_ticker_detail_{id(stub_loader)}", str(_LIVE / "ticker_detail.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ── Loader additions ─────────────────────────────────────────────────────────


def test_load_universe_archive_key_and_empty_ticker():
    loader = _load_live_loader()
    captured = {}

    def fake_dl(bucket, key):
        captured["bucket"], captured["key"] = bucket, key
        return {"key_catalyst": "x"}

    with patch.object(loader, "download_s3_json", side_effect=fake_dl), \
         patch.object(loader, "_research_bucket", return_value="b"):
        assert loader.load_universe_archive("AAPL") == {"key_catalyst": "x"}
        assert captured["key"] == "archive/universe/AAPL/thesis.json"
        # Empty ticker short-circuits to None without an S3 call.
        assert loader.load_universe_archive("") is None


def test_load_order_book_rationale_key():
    loader = _load_live_loader()
    captured = {}

    def fake_dl(bucket, key):
        captured["key"] = key
        return {"considered": []}

    with patch.object(loader, "download_s3_json", side_effect=fake_dl), \
         patch.object(loader, "_research_bucket", return_value="b"):
        assert loader.load_order_book_rationale() == {"considered": []}
        assert captured["key"] == "trades/order_book_rationale/latest.json"


# ── ticker_detail pure helpers ────────────────────────────────────────────────


@pytest.fixture
def td():
    stub = MagicMock()
    stub.load_latest_signals.return_value = {
        "universe": [
            {"ticker": "AAPL", "score": 78, "sector": "Tech", "thesis_summary": "strong"},
            {"ticker": "MSFT", "score": 71},
        ]
    }
    stub.load_order_book_rationale.return_value = {
        "considered": [{"ticker": "AAPL", "decision": "HOLD"}]
    }
    stub.load_predictions_json.return_value = {"AAPL": {"predicted_direction": "UP"}}
    stub.load_universe_archive.return_value = {"key_catalyst": "earnings"}
    return _load_ticker_detail(stub)


def test_fmt_pct(td):
    assert td._fmt_pct(0.0234) == "+2.3%"
    assert td._fmt_pct(-0.10) == "-10.0%"
    assert td._fmt_pct(None) == "—"
    assert td._fmt_pct("nan-ish") == "—"


def test_position_info_dict_list_missing(td):
    assert td._position_info("AAPL", {"AAPL": {"shares": 10}}) == {"shares": 10}
    assert td._position_info("MSFT", [{"ticker": "MSFT", "shares": 5}]) == {"ticker": "MSFT", "shares": 5}
    assert td._position_info("X", {}) == {}
    assert td._position_info("X", None) == {}


def test_signals_entry_lookup(td):
    assert td._signals_entry("AAPL")["score"] == 78
    assert td._signals_entry("ZZZZ") == {}


def test_obr_block_lookup(td):
    assert td._obr_block("AAPL") == {"ticker": "AAPL", "decision": "HOLD"}
    assert td._obr_block("MSFT") is None
