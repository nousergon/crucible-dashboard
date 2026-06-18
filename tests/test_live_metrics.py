"""Tests for live/shared.py::compute_live_metrics — the derivation behind
the Live Portfolio intraday header.

The producer (daemon) publishes RAW marks in intraday/nav.json; this is
where today's return + alpha-vs-SPY are derived against the prior EOD
close, so the logic (staleness gate, baseline selection, alpha math) is
worth pinning here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timezone

import pytest

pytest.importorskip("pandas")
import pandas as pd  # noqa: E402

# live/shared.py does `from loaders.s3_loader import load_eod_pnl` at module
# load, and live/loaders/s3_loader.py reads config.yaml in its decorators.
# We only need the pure derivation (compute_live_metrics), so load shared.py
# by file path with a transient stub for that loader import — and restore
# sys.modules afterward so we don't shadow the real loaders.s3_loader that
# many other tests import (the repo has both a top-level and a live/ copy).
_SHARED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "live", "shared.py")
_saved = {k: sys.modules.get(k) for k in ("loaders", "loaders.s3_loader")}
_pkg = types.ModuleType("loaders")
_pkg.__path__ = []  # mark as a package
_sub = types.ModuleType("loaders.s3_loader")
_sub.load_eod_pnl = lambda: None
sys.modules["loaders"] = _pkg
sys.modules["loaders.s3_loader"] = _sub
try:
    _spec = importlib.util.spec_from_file_location("live_shared_under_test", _SHARED)
    _shared = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_shared)
finally:
    for _k, _v in _saved.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v

EodPrep = _shared.EodPrep
LiveMetrics = _shared.LiveMetrics
compute_live_metrics = _shared.compute_live_metrics


def _prep() -> EodPrep:
    eod = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
            "portfolio_nav": [1_010_630.61, 1_000_564.85],
            "spy_close": [750.33, 740.96],
        }
    )
    return EodPrep(
        eod=eod,
        eod_active=eod,
        latest=eod.iloc[-1],
        nav=1_000_564.85,
        inception_date=eod["date"].iloc[0],
        cumulative_alpha_bps=0.0,
        up_days=0,
        down_days=0,
        total_days=0,
        perf_date="2026-06-17",
    )


_NOW = datetime(2026, 6, 18, 18, 38, 0, tzinfo=timezone.utc)


def _nav(**over) -> dict:
    base = {
        "timestamp": "2026-06-18T18:37:30.000Z",
        "ib_connected": True,
        "net_liquidation": 1_005_000.0,
        "spy_last": 745.0,
    }
    base.update(over)
    return base


class TestLive:
    def test_fresh_snapshot_derives_return_and_alpha(self):
        m = compute_live_metrics(_nav(), _prep(), now_utc=_NOW)
        assert isinstance(m, LiveMetrics)
        assert m.nav == 1_005_000.0
        # baseline = 2026-06-17 close (1_000_564.85 / 740.96)
        assert m.day_return == pytest.approx(1_005_000.0 / 1_000_564.85 - 1)
        assert m.spy_return == pytest.approx(745.0 / 740.96 - 1)
        assert m.day_alpha == pytest.approx(m.day_return - m.spy_return)
        assert m.as_of_et == "2:37 PM ET"  # 18:37 UTC → 14:37 EDT

    def test_missing_spy_mark_yields_return_but_no_alpha(self):
        m = compute_live_metrics(_nav(spy_last=None), _prep(), now_utc=_NOW)
        assert m is not None
        assert m.day_return == pytest.approx(1_005_000.0 / 1_000_564.85 - 1)
        assert m.spy_return is None
        assert m.day_alpha is None

    def test_baseline_is_strictly_prior_trading_day(self):
        # A snapshot dated 2026-06-17 (already booked in eod) measures
        # against 2026-06-16, never against its own same-day close.
        nav = _nav(timestamp="2026-06-17T18:37:30.000Z")
        now = datetime(2026, 6, 17, 18, 38, 0, tzinfo=timezone.utc)
        m = compute_live_metrics(nav, _prep(), now_utc=now)
        assert m is not None
        assert m.day_return == pytest.approx(1_005_000.0 / 1_010_630.61 - 1)


class TestNotLive:
    def test_none_snapshot(self):
        assert compute_live_metrics(None, _prep(), now_utc=_NOW) is None

    def test_none_prep(self):
        assert compute_live_metrics(_nav(), None, now_utc=_NOW) is None

    def test_disconnected(self):
        assert compute_live_metrics(_nav(ib_connected=False), _prep(), now_utc=_NOW) is None

    def test_missing_nav(self):
        assert compute_live_metrics(_nav(net_liquidation=None), _prep(), now_utc=_NOW) is None

    def test_stale_snapshot(self):
        # 10 min old → past the 5 min staleness gate.
        stale = _nav(timestamp="2026-06-18T18:28:00.000Z")
        assert compute_live_metrics(stale, _prep(), now_utc=_NOW) is None

    def test_future_timestamp_rejected(self):
        future = _nav(timestamp="2026-06-18T18:40:00.000Z")
        assert compute_live_metrics(future, _prep(), now_utc=_NOW) is None

    def test_unparseable_timestamp(self):
        assert compute_live_metrics(_nav(timestamp="not-a-date"), _prep(), now_utc=_NOW) is None

    def test_no_prior_baseline(self):
        # Snapshot dated on/before the earliest EOD row → no prior close.
        nav = _nav(timestamp="2026-06-16T18:37:30.000Z")
        now = datetime(2026, 6, 16, 18, 38, 0, tzinfo=timezone.utc)
        assert compute_live_metrics(nav, _prep(), now_utc=now) is None
