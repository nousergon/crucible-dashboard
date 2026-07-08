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

    def test_trust_carries_legs_and_findings(self):
        with _patched():
            body = client.get("/api/trust").json()
        assert len(body["legs"]) >= 7
        assert all(r["ci"] == "SUCCESS" for r in body["legs"])
        assert all("fix" in f for f in body["findings"])
