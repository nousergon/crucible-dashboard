"""Consumer-wiring tests for the morning brief (config#664 / L4574).

Covers the impure shell WITHOUT live data or an Anthropic key, by mocking the
Anthropic SDK + S3 and stubbing ``loaders.s3_loader`` (streamlit is mocked in
conftest). Uses the importlib-from-file isolation pattern (mirrors
tests/test_ticker_detail.py) so loading the ``live/`` modules does not pollute
``sys.modules['loaders']`` for the rest of the suite — ``live/loaders`` and the
top-level ``loaders`` are both packages named ``loaders``.

Covered:
  * ``generate_morning_brief`` parses Haiku text blocks, uses claude-haiku-4-5,
    and sends no thinking/effort params (Haiku rejects them).
  * ``generate_morning_brief`` is fail-soft (None) with no key / on SDK error.
  * the ``ai_advisor.enabled`` kill switch suppresses generation.
  * ``top_holdings_news`` ranks/filters per-ticker rows (pure).
  * ``load_daily_news_rows`` is fail-soft to [] when the sidecar is missing.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas  # noqa: F401 — preload so the open-mock below doesn't shadow it
import pytest
import yaml  # noqa: F401

_ROOT = Path(__file__).parent.parent
_LIVE = _ROOT / "live"
# morning_brief_cadence is pure (no loaders import) — make it importable.
if str(_LIVE) not in sys.path:
    sys.path.insert(0, str(_LIVE))

from morning_brief_cadence import MarketSnapshot  # noqa: E402

ET = ZoneInfo("America/New_York")

_STUB_CFG = {
    "s3": {"research_bucket": "test", "trades_bucket": "test"},
    "cache_ttl": {"research": 3600, "trades": 900},
    "paths": {"eod_pnl": "trades/eod_pnl.csv"},
}


def _stub_s3_loader():
    """A minimal stand-in for live/loaders/s3_loader exposing only what the
    morning-brief modules import at module-exec time."""
    stub = type(sys)("loaders.s3_loader")
    stub.get_s3_client = MagicMock()
    stub._research_bucket = lambda: "test"
    stub._ttl = lambda key: _STUB_CFG["cache_ttl"].get(key, 900)
    stub.load_config = lambda: _STUB_CFG
    stub.load_intraday_nav = lambda: None
    stub.load_live_day_return = lambda t: None
    return stub


def _load_live_module(relpath: str, modname: str, stub_loader):
    """Load a live/ module via importlib with ``loaders.s3_loader`` stubbed and
    isolated, restoring sys.modules afterward."""
    saved = {
        k: sys.modules.get(k)
        for k in ("loaders", "loaders.s3_loader", "loaders.daily_news",
                  "loaders.market_snapshot")
    }
    pkg = type(sys)("loaders")
    pkg.__path__ = [str(_LIVE / "loaders")]  # allow submodule discovery
    pkg.s3_loader = stub_loader
    sys.modules["loaders"] = pkg
    sys.modules["loaders.s3_loader"] = stub_loader
    try:
        spec = importlib.util.spec_from_file_location(
            f"{modname}_{id(stub_loader)}", str(_LIVE / relpath)
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


def _daily_news():
    return _load_live_module("loaders/daily_news.py", "mb_daily_news", _stub_s3_loader())


def _morning_brief():
    return _load_live_module("morning_brief.py", "mb_morning_brief", _stub_s3_loader())


def _snap():
    return MarketSnapshot(
        ts=datetime(2026, 6, 18, 9, 30, tzinfo=ET),
        spy_day_return_pp=-1.2,
        qqq_day_return_pp=-1.5,
        vix=22.0,
    )


# ── top_holdings_news (pure ranking/filter) ────────────────────────────────


class TestTopHoldingsNews:
    def _rows(self):
        return [
            {"ticker": "AAPL", "n_articles": 5, "event_severity_max": 0.9,
             "lm_sentiment_trusted_mean": -0.3, "event_count": 2},
            {"ticker": "MSFT", "n_articles": 2, "event_severity_max": 0.1,
             "lm_sentiment_trusted_mean": 0.05, "event_count": 0},
            {"ticker": "ZZZZ", "n_articles": 0, "event_severity_max": 0.0,
             "lm_sentiment_trusted_mean": 0.0, "event_count": 0},  # no signal
        ]

    def test_ranks_by_severity_then_volume(self):
        dn = _daily_news()
        out = dn.top_holdings_news(self._rows())
        assert [r["ticker"] for r in out] == ["AAPL", "MSFT"]

    def test_filters_to_held_tickers(self):
        dn = _daily_news()
        out = dn.top_holdings_news(self._rows(), held_tickers={"MSFT"})
        assert [r["ticker"] for r in out] == ["MSFT"]

    def test_respects_limit(self):
        dn = _daily_news()
        out = dn.top_holdings_news(self._rows(), limit=1)
        assert len(out) == 1 and out[0]["ticker"] == "AAPL"


# ── load_daily_news_rows fail-soft ─────────────────────────────────────────


class TestDailyNewsReader:
    def test_missing_sidecar_returns_empty(self):
        dn = _daily_news()
        client = MagicMock()
        err = Exception("nope")
        err.response = {"Error": {"Code": "NoSuchKey"}}
        client.get_object.side_effect = err
        with patch.object(dn, "get_s3_client", return_value=client), \
             patch.object(dn, "_research_bucket", return_value="bkt"):
            rows = dn.load_daily_news_rows()
        assert rows == []


# ── generate_morning_brief (mocked Anthropic) ──────────────────────────────


class TestGenerateBrief:
    def test_parses_text_blocks_and_uses_haiku(self):
        mb = _morning_brief()
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="Macro lead.\n- AAPL: news")]
                )

        class FakeClient:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

        with patch.dict(sys.modules, {"anthropic": SimpleNamespace(Anthropic=FakeClient)}):
            text = mb.generate_morning_brief(
                _snap(), [{"ticker": "AAPL", "n_articles": 3}], api_key="sk-test"
            )
        assert text == "Macro lead.\n- AAPL: news"
        assert captured["model"] == "claude-haiku-4-5"
        assert "thinking" not in captured
        assert "output_config" not in captured

    def test_no_key_returns_none(self):
        mb = _morning_brief()
        with patch.object(mb, "_anthropic_api_key", return_value=None):
            assert mb.generate_morning_brief(_snap(), [], api_key=None) is None

    def test_sdk_error_is_fail_soft(self):
        mb = _morning_brief()

        class FakeClient:
            def __init__(self, **kwargs):
                raise RuntimeError("boom")

        with patch.dict(sys.modules, {"anthropic": SimpleNamespace(Anthropic=FakeClient)}):
            assert mb.generate_morning_brief(_snap(), [], api_key="sk-test") is None


# ── kill switch ────────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_disabled_suppresses_generation(self):
        mb = _morning_brief()
        with patch.object(mb, "load_config", return_value={"ai_advisor": {"enabled": False}}):
            assert mb._ai_advisor_enabled() is False
            out = mb.get_or_generate_brief(held_tickers=set())
        assert out["enabled"] is False
        assert out["brief_text"] is None

    def test_enabled_by_default_when_absent(self):
        mb = _morning_brief()
        with patch.object(mb, "load_config", return_value={}):
            assert mb._ai_advisor_enabled() is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
