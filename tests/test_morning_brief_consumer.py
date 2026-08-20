"""Consumer-wiring tests for the morning brief (config#664 / L4574).

Covers the impure shell WITHOUT live data or a router credential, by
injecting a fake ``krepis.llm.LLMClient`` transport (``client_factory`` test
seam) + S3 and stubbing ``loaders.s3_loader`` (streamlit is mocked in
conftest). Uses the importlib-from-file isolation pattern (mirrors
tests/test_ticker_detail.py) so loading the ``live/`` modules does not
pollute ``sys.modules['loaders']`` for the rest of the suite — ``live/loaders``
and the top-level ``loaders`` are both packages named ``loaders``.

Covered (post alpha-engine-config-I6367/I7879 router migration, 2026-08-20):
  * ``generate_morning_brief`` addresses the ``low`` group from ``exec_context
    "ec2"`` and hands the router's resolved spec straight to ``LLMClient``
    (mirrors ``crucible-research/producers/single_agent.py``'s
    ``_patch_router`` pattern: fake the REGISTRY READ
    (``resolve_group_structured``), not the resolver under test).
  * ``generate_morning_brief`` is fail-soft (None) when router resolution
    raises (router/group unreachable) or the transport call fails — and logs
    a WARNING naming the failed dependency either way (model-router-policy
    §3.4 R20 governs the resolver; this module's own None-return is the
    documented UI-convenience deviation from fail-loud, not R20 itself).
  * no direct-provider ``ModelSpec`` is ever constructed in this module (AST
    check, mirrors crucible-research's
    ``test_module_constructs_no_provider_pinned_spec``).
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


# ── generate_morning_brief (fake krepis.router registry read + LLMClient
#    transport — alpha-engine-config-I6367/I7879) ───────────────────────────


def _fake_route(**over):
    """What ``krepis.router.resolve_group_structured`` returns for the
    ``low`` group from ``ec2``: a direct egress-proxy route, since both
    ``low`` members declare ``reachable_from: [laptop, ec2]`` in
    LLM_MODEL_REGISTRY.yaml (unlike a Lambda-only group, this one does NOT
    need the synthesised ``litellm_proxy`` route to be reachable)."""
    route = {
        "schema_version": 2,
        "group": "low",
        "route": "egress_proxy",
        "provider": "deepseek",
        "deployment_id": "deepseek-v4-flash-low",
        "api_base_url": "http://127.0.0.1:8990",
        "auth_token_type": "placeholder",
        "registry_id": "deepseek-v4-flash-low",
        "primary_registry_id": "deepseek-v4-flash-low",
        "params": {"max_tokens": 8192, "reasoning": {"effort": "low"}},
    }
    route.update(over)
    return route


def _patch_router(monkeypatch, *, route=None, captured=None):
    """Fake the REGISTRY READ, not the resolver under test.

    Mirrors ``crucible-research/tests/test_single_agent_producer.py``'s
    ``_patch_router``: ``resolve_group_spec`` (the thing this call site
    calls) is left real, so its ModelSpec-building and auth-type mapping
    stay covered. Only ``resolve_group_structured`` — krepis' own registry
    read — is replaced.
    """
    import krepis.router as _kr

    the_route = route or _fake_route()

    def fake_resolve_structured(group, *, exec_context=None, wire="openai"):
        if captured is not None:
            captured.append(
                {"group": group, "exec_context": exec_context, "wire": wire}
            )
        return the_route

    monkeypatch.setattr(_kr, "resolve_group_structured", fake_resolve_structured)


class TestGenerateBrief:
    def test_addresses_low_group_from_ec2_and_routes_the_spec(self, monkeypatch):
        mb = _morning_brief()
        captured_resolves = []
        _patch_router(monkeypatch, captured=captured_resolves)
        captured_specs = []
        captured_kwargs = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured_kwargs.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content="Macro lead.\n- AAPL: news")
                    )],
                    model=kwargs["model"],
                    usage=None,
                )

        class FakeChat:
            def __init__(self):
                self.completions = FakeCompletions()

        class FakeClient:
            def __init__(self):
                self.chat = FakeChat()

        def factory(spec, api_key):
            captured_specs.append(spec)
            return FakeClient()

        text = mb.generate_morning_brief(
            _snap(), [{"ticker": "AAPL", "n_articles": 3}],
            client_factory=factory,
        )
        assert text == "Macro lead.\n- AAPL: news"

        # The call site declares only group + exec_context + wire.
        assert captured_resolves == [
            {"group": "low", "exec_context": "ec2", "wire": "openai"}
        ]
        # The registry's route decided model, provider and reasoning — none
        # of it is held in this module.
        spec = captured_specs[0]
        assert spec.model == "deepseek-v4-flash-low"
        assert spec.provider == "deepseek"
        assert spec.reasoning == {"effort": "low"}
        assert captured_kwargs["model"] == "deepseek-v4-flash-low"

    def test_router_resolution_failure_is_fail_soft_and_warns(self, monkeypatch, caplog):
        mb = _morning_brief()
        import krepis.router as _kr

        def _boom(group, *, exec_context=None, wire="openai"):
            raise RuntimeError("no model in group 'low' is reachable from ec2")

        monkeypatch.setattr(_kr, "resolve_group_structured", _boom)

        with caplog.at_level("WARNING"):
            out = mb.generate_morning_brief(_snap(), [])
        assert out is None
        assert any(
            "router resolution failed" in r.message and "group=low" in r.message
            for r in caplog.records
        )

    def test_transport_error_is_fail_soft(self, monkeypatch):
        mb = _morning_brief()
        _patch_router(monkeypatch)

        class FakeClient:
            def __init__(self):
                raise RuntimeError("boom")

        assert mb.generate_morning_brief(
            _snap(), [],
            client_factory=lambda spec, api_key: FakeClient(),
        ) is None

    def test_module_constructs_no_direct_provider_spec(self):
        """Structural, not textual. What must not come back is a
        ``ModelSpec(...)`` built here, or a binding of any provider API-key
        name (Brian's 2026-08-03 ruling, alpha-engine-config-I6367).

        The key names are assembled rather than written out: this file is
        scanned by the fleet's direct-linkage guard, and a test asserting a
        literal's ABSENCE is indistinguishable from a call site using it, so
        spelling it here would earn an allowlist entry for a test that exists
        to prove the entry is unnecessary."""
        import ast

        src = _LIVE / "morning_brief.py"
        tree = ast.parse(src.read_text())

        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "ModelSpec" not in called, (
            "live/morning_brief.py constructs a ModelSpec directly — model, "
            "endpoint and credential are registry decisions resolved by "
            "krepis.router.resolve_group_spec (alpha-engine-config-I6367)"
        )

        bound = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        for provider in ("OPENROUTER", "ANTHROPIC", "DEEPSEEK"):
            key_name = f"{provider}_API_KEY"
            assert key_name not in bound, (
                f"live/morning_brief.py binds {key_name} — the credential is "
                f"a registry decision resolved by krepis.router, and a "
                f"provider key on this path is the direct linkage Brian's "
                f"2026-08-03 ruling removed"
            )


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
