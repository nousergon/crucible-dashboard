"""Calendar-aware freshness history (alpha-engine-config#1984 item 7).

``views/26_Artifact_Freshness.py``'s 12-week history drill-down used to mark
every ``present: false`` cycle as "❌ absent" — including NYSE weekends and
holidays, which are correct absences, not failures (acknowledged in a
now-updated code comment). ``_is_non_trading_day`` is the fix: a small,
streamlit-free helper checked directly via exec-load, mirroring the
``tests/test_pipeline_status_page.py`` / ``tests/test_book_status_banner.py``
convention (streamlit mocked, S3 loaders stubbed to no-ops, page exec'd once
per test module).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "26_Artifact_Freshness.py"


@pytest.fixture
def page_mod(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT))

    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    mock_st.columns.side_effect = lambda spec, **kw: [
        MagicMock() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))
    ]
    mock_st.multiselect.side_effect = lambda label, options, default=None, **kw: default
    mock_st.session_state = {}
    mock_st.stop = lambda: sys.exit("st.stop() called during page exec (expected in error paths)")
    monkeypatch.setitem(sys.modules, "streamlit", mock_st)

    # tests/test_db_loader.py permanently replaces sys.modules["loaders.s3_loader"]
    # with a bare MagicMock() (no restoration) — force a fresh real import if a
    # sibling test module left that behind, mirroring the same guard in
    # tests/test_regime_eval_loaders.py's `loader` fixture.
    for _mod_name in ("loaders.s3_loader", "loaders"):
        _cached = sys.modules.get(_mod_name)
        if _cached is not None and isinstance(_cached, MagicMock):
            del sys.modules[_mod_name]

    # Minimal valid heartbeat + check_results + history so the module execs
    # all the way through without network calls or an early st.stop() (which
    # is a no-op on the mock, so execution would otherwise fall through into
    # AttributeErrors on None). Patched via monkeypatch (auto-restored after
    # the test) rather than a permanent module-attribute overwrite — these
    # are shared s3_loader internals other tests' loader calls depend on.
    from loaders import s3_loader

    _heartbeat = {"last_run": "2026-07-09T12:00:00Z", "counts": {}, "alerts_enabled": True}
    _check_results = {
        "results": [
            {
                "artifact_id": "signals_daily",
                "owner_repo": "crucible-research",
                "cadence": "daily",
                "severity": "critical",
                "state": "fresh",
                "canonical_key": "signals/{date}/signals.json",
                "last_modified": "2026-07-09T12:00:00Z",
                "sla_violated_by_minutes": 0,
                "recovery_substituted": False,
                "reason": "",
            }
        ]
    }
    _history = {
        "generated_at": "2026-07-09T04:00:00Z",
        "lookback": {"cycles": 5},
        "artifacts": {},
    }

    def _fake_fetch(bucket, key):
        if key.endswith("heartbeat.json"):
            return _heartbeat
        if key.endswith("check_results.json"):
            return _check_results
        if key.endswith("history.json"):
            return _history
        return None

    monkeypatch.setattr(s3_loader, "_fetch_s3_json", _fake_fetch)
    monkeypatch.setattr(s3_loader, "_research_bucket", lambda: "test-bucket")
    import sys as _sys_dbg
    print("DEBUG s3_loader id:", id(s3_loader), "sys.modules id:", id(_sys_dbg.modules.get("loaders.s3_loader")), "same:", s3_loader is _sys_dbg.modules.get("loaders.s3_loader"), "fetch is fake:", s3_loader._fetch_s3_json is _fake_fetch, file=_sys_dbg.stderr)

    from loaders import observation_registry_loader as orl
    monkeypatch.setattr(orl, "load_observation_registry", lambda: None)

    spec = importlib.util.spec_from_file_location("_artifact_freshness_under_test", PAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import sys as _sys_dbg2
    print("DEBUG page mod._fetch_s3_json is fake:", mod._fetch_s3_json is _fake_fetch, "page mod._research_bucket():", mod._research_bucket(), file=_sys_dbg2.stderr)
    return mod


class TestIsNonTradingDay:
    def test_saturday_is_non_trading(self, page_mod):
        assert page_mod._is_non_trading_day("2026-07-04") is True  # Saturday

    def test_nyse_holiday_is_non_trading(self, page_mod):
        assert page_mod._is_non_trading_day("2026-01-19") is True  # MLK Day 2026

    def test_normal_weekday_is_trading(self, page_mod):
        assert page_mod._is_non_trading_day("2026-07-09") is False  # Thursday, no holiday

    def test_none_is_treated_as_trading_day(self, page_mod):
        # No date to check against — don't hide a possibly-real gap.
        assert page_mod._is_non_trading_day(None) is False

    def test_unparseable_date_is_treated_as_trading_day(self, page_mod):
        assert page_mod._is_non_trading_day("not-a-date") is False

    def test_accepts_full_iso_timestamp(self, page_mod):
        # Some history entries may carry a full timestamp rather than a bare
        # date; the first 10 chars (YYYY-MM-DD) must still parse.
        assert page_mod._is_non_trading_day("2026-07-04T00:00:00Z") is True


class TestHistoryDrilldownBadging:
    def test_holiday_absence_is_not_marked_missing(self, page_mod):
        entry = {"cadence": "daily", "history": [
            {"date": "2026-01-19", "present": False},  # MLK Day
        ]}
        rows = []
        for c in entry["history"]:
            c_date = c.get("date")
            if c.get("present"):
                presence = "✅"
            elif page_mod._is_non_trading_day(c_date):
                presence = "➖ non-trading day"
            else:
                presence = "❌"
            rows.append(presence)
        assert rows == ["➖ non-trading day"]

    def test_genuine_weekday_absence_still_flagged(self, page_mod):
        entry = {"cadence": "daily", "history": [
            {"date": "2026-07-09", "present": False},  # real Thursday miss
        ]}
        rows = []
        for c in entry["history"]:
            c_date = c.get("date")
            if c.get("present"):
                presence = "✅"
            elif page_mod._is_non_trading_day(c_date):
                presence = "➖ non-trading day"
            else:
                presence = "❌"
            rows.append(presence)
        assert rows == ["❌"]
