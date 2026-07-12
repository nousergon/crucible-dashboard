"""Contract tests for dash_api (config#1973 phase 9-B).

These response shapes are the contract the Next.js frontend's fixtures
mirror — a shape change here is a frontend-breaking change and must be
deliberate. Loaders are stubbed at the dash_api.main namespace (the app
imports them by name); streamlit is mocked before import per repo pattern.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from fastapi.testclient import TestClient  # noqa: E402

from dash_api import main as api  # noqa: E402

client = TestClient(api.app, raise_server_exceptions=False)

_CARD = {
    "_provenance": {"run_date": "2026-07-04", "grader_source": "evaluator"},
    "tiles": {
        "research": {"status": "RED", "components": [
            {"name": "scanner", "criticality": "critical", "status": "RED",
             "status_reason": "scanner IC negative"},
        ]},
        "substrate": {"status": "WATCH", "components": []},  # ops — must not leak
    },
}
_EOD = pd.DataFrame({
    "date": ["2026-03-09", "2026-03-10"],
    "daily_return_pct": [1.0, -0.5],
    "spy_return_pct": [0.5, 0.1],
    "daily_alpha_pct": [0.5, -0.6],
})


def _patched(**over):
    defaults = {
        "load_report_card": lambda: _CARD,
        "list_backtest_dates": lambda: ["2026-07-04"],
        "load_backtest_file": lambda d, f: None,
        "load_eod_pnl": lambda: _EOD,
        "load_ci_verdicts": lambda repos: {r: {"conclusion": "success"} for r in repos},
    }
    defaults.update(over)
    return patch.multiple(api, **defaults)


class TestEndpoints:
    def test_health(self):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_experiment_identity_shape(self):
        with _patched():
            body = client.get("/api/experiment").json()
        assert body["experiment_id"] == "reference-rate"
        assert body["report_card_date"] == "2026-07-04"
        assert all(set(s) == {"slot", "impl"} for s in body["slots"])

    def test_headline_is_the_five_stat_strip(self):
        with _patched():
            body = client.get("/api/headline").json()
        assert [s["label"] for s in body] == [
            "Alpha vs SPY (cum)", "Sharpe (ann.)", "PSR", "Hit rate · 21d", "Max drawdown",
        ]
        assert all({"value", "sub", "help"} <= set(s) for s in body)

    def test_equity_series_json_safe(self):
        with _patched():
            body = client.get("/api/equity").json()
        assert len(body) == 2
        assert set(body[0]) == {"date", "Portfolio", "SPY"}
        assert isinstance(body[0]["date"], str)

    def test_alpha_periods_validates_period(self):
        with _patched():
            assert client.get("/api/alpha-periods?period=W").status_code == 200
            assert client.get("/api/alpha-periods?period=Q").status_code == 422

    def test_verdicts_exclude_ops_tiles(self):
        with _patched():
            body = client.get("/api/verdicts").json()
        assert [r["tile"] for r in body] == ["research"]

    def test_tile_detail_enforces_audience_split_with_404(self):
        with _patched():
            assert client.get("/api/tiles/research").status_code == 200
            # Ops tiles are structurally unreachable — not frontend discipline.
            assert client.get("/api/tiles/substrate").status_code == 404
            assert client.get("/api/tiles/agent").status_code == 404

    def test_absent_artifacts_yield_honest_rows_not_500(self):
        with _patched():
            integrity = client.get("/api/integrity")
        assert integrity.status_code == 200
        assert all(r["status"] == "ABSENT" for r in integrity.json())

    def test_hard_loader_failure_is_503_not_empty_200(self):
        def boom():
            raise RuntimeError("s3 unreachable")
        with _patched(load_report_card=boom):
            resp = client.get("/api/verdicts")
        assert resp.status_code == 503
        assert "RuntimeError" in resp.json()["detail"]

    def test_s3_access_denied_is_503_with_error_taxonomy(self):
        # config#2339 acceptance: a real S3AccessError (what s3_loader now
        # raises for a non-NoSuchKey ClientError under _guard's strict mode)
        # must map to a 503 carrying an error taxonomy — not the pre-fix
        # behavior of a swallowed None rendering as an honest-looking 200.
        from loaders.s3_loader import S3AccessError

        def boom():
            raise S3AccessError(
                "S3 AccessDenied for research-bucket/evaluator/latest/report_card.json",
                error_type="ClientError:AccessDenied",
                bucket="research-bucket",
                key="evaluator/latest/report_card.json",
            )
        with _patched(load_report_card=boom):
            resp = client.get("/api/verdicts")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "S3AccessError" in detail
        assert "AccessDenied" in detail

    def test_no_such_key_stays_200_absent_via_real_loader_path(self):
        # config#2339 acceptance, exercised one level below the boto3
        # boundary (patching _s3_get_object itself, the same proven-stable
        # interception point every other s3_loader test in this repo uses —
        # see tests/test_s3_loader_core.py's TestFetchS3Json/TestDownloadS3*):
        # NoSuchKey through the REAL loaders.s3_loader.load_report_card ->
        # download_s3_json -> _fetch_s3_json chain, run under _guard's
        # strict mode, must still come back as honest-ABSENT (200), never a
        # 503 — strict mode only changes behavior for non-NoSuchKey
        # ClientErrors, and this proves that end to end through the real
        # (non-stubbed) loader call chain.
        #
        # Patches land on api.load_report_card's OWN __globals__ (the
        # module dict the function actually resolves _s3_get_object /
        # get_latest_prefix from at call time), not a fresh
        # `import loaders.s3_loader`. Some other test file in this suite
        # (e.g. test_process_archive.py, test_artifact_freshness_page.py)
        # calls importlib.reload(s3_loader) for its own isolation needs —
        # that replaces every function object in sys.modules
        # ['loaders.s3_loader'] with a new one, but dash_api.main's
        # `from loaders.s3_loader import load_report_card` (bound at
        # dash_api.main's own import time) keeps pointing at the
        # pre-reload function object forever after. So `api.load_report_card
        # is sys.modules['loaders.s3_loader'].load_report_card` can be
        # False depending on test order — going through
        # api.load_report_card.__globals__ instead is correct regardless.
        s3_loader_globals = api.load_report_card.__globals__

        with patch.dict(s3_loader_globals, {
            "_s3_get_object": lambda bucket, key: None,
            "get_latest_prefix": lambda bucket, prefix: None,
        }):
            # _s3_get_object -> None is exactly NoSuchKey's outcome (see
            # _s3_get_object's own docstring); get_latest_prefix -> None
            # short-circuits load_report_card's date resolution to the same
            # "nothing published yet" honest-ABSENT path without needing a
            # live bucket config.
            resp = client.get("/api/verdicts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_trust_carries_legs_and_findings(self):
        with _patched():
            body = client.get("/api/trust").json()
        assert len(body["legs"]) >= 7
        assert all(r["ci"] == "SUCCESS" for r in body["legs"])
        assert all("fix" in f for f in body["findings"])


class TestIntradayEndpoint:
    def test_absent_intraday_is_honest_empty_not_error(self):
        with _patched():
            with patch.object(api, "load_intraday_nav", lambda: None):
                resp = client.get("/api/intraday")
        assert resp.status_code == 200
        assert resp.json() == []
