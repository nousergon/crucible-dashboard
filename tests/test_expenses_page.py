"""Expenses page contracts: tab registration on the Cost & Usage host, the
``expenses/latest.json`` loader wiring, and the pure view-model helpers in
``shared/expense_view.py`` (formatting, spend-first ordering, badge mapping,
staleness derivation). Mirrors ``tests/test_decision_queue_page.py``'s
source-assertion style for the registration contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from shared.expense_view import (
    RECONCILIATION_DELTA_PCT_THRESHOLD,
    as_of_age_hours,
    error_rows,
    pace_badge,
    provider_table_rows,
    quota_str,
    reconciliation_table_rows,
    usd,
)

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "50_Expenses.py"

DOC = {
    "schema_version": 1,
    "period": "2026-07",
    "as_of": "2026-07-17T12:15:00+00:00",
    "month_elapsed_frac": 0.53,
    "providers": [
        {"key": "aws", "label": "AWS", "status": "ok", "mtd_cost_usd": 12.34,
         "projected_month_end_usd": 22.34, "budget_usd": 50.0, "pace": "under",
         "quota": None, "note": None, "error": None},
        {"key": "claude_max", "label": "Claude Max 20x subscription",
         "status": "fixed", "mtd_cost_usd": 200.0,
         "projected_month_end_usd": 200.0, "budget_usd": 200.0, "pace": "fixed",
         "quota": None, "note": None, "error": None},
        {"key": "github_org", "label": "GitHub (nousergon org)", "status": "ok",
         "mtd_cost_usd": 0.0, "projected_month_end_usd": 0.0, "budget_usd": None,
         "pace": "over",
         "quota": {"unit": "Actions minutes", "used": 1400, "limit": 2000,
                   "projected": 2630}, "note": None, "error": None},
        {"key": "deepseek", "label": "DeepSeek", "status": "error",
         "mtd_cost_usd": None, "projected_month_end_usd": None,
         "budget_usd": None, "pace": None, "quota": None, "note": None,
         "error": "HTTP 500 from api.deepseek.com"},
        {"key": "neon", "label": "Neon Postgres", "status": "not_configured",
         "mtd_cost_usd": None, "projected_month_end_usd": None,
         "budget_usd": None, "pace": None, "quota": None, "note": None,
         "error": "SSM /alpha-engine/NEON_API_KEY missing"},
    ],
    "totals": {"mtd_usd": 212.34, "projected_usd": 222.34, "budget_usd": 250.0,
               "incomplete": True},
    "warnings": [],
}

RECON_DOC = {
    "schema_version": 1,
    "period": "2026-06",
    "as_of": "2026-07-02T03:00:00+00:00",
    "delta_pct_threshold": 0.08,
    "flagged": ["aws"],
    "providers": {
        "aws": {"projected_last_seen": 20.0, "accrued_mtd_final": 40.0,
                "actual_final": 55.0, "delta_usd": 15.0, "delta_pct": 0.375,
                "status": "ok", "note": None},
        "openrouter": {"projected_last_seen": 12.0, "accrued_mtd_final": 11.0,
                       "actual_final": 11.2, "delta_usd": 0.2, "delta_pct": 0.018,
                       "status": "ok", "note": None},
        "neon": {"projected_last_seen": None, "accrued_mtd_final": None,
                 "actual_final": None, "delta_usd": None, "delta_pct": None,
                 "status": "not_available",
                 "note": "Neon's API exposes only the current consumption period"},
    },
}


class TestRegistration:
    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_host_registers_expenses_tab_first(self):
        host_src = (REPO_ROOT / "views" / "host_cost_usage.py").read_text()
        assert '("Expenses", "50_Expenses.py")' in host_src
        # First tab = default view when ?tab= is absent — the money headline.
        assert host_src.index("50_Expenses.py") < host_src.index("23_LLM_Cost.py")

    def test_loader_reads_latest_key(self):
        loader_src = (REPO_ROOT / "loaders" / "s3_loader.py").read_text()
        assert "def load_expense_report" in loader_src
        assert '"expenses/latest.json"' in loader_src

    def test_page_uses_loader_and_view_model(self):
        page_src = PAGE.read_text()
        assert "load_expense_report" in page_src
        assert "provider_table_rows" in page_src

    def test_page_renders_gha_public_private_breakdown(self):
        # Guardrail against a real 2026-07-17 incident: a repo's public/private
        # GHA minutes were misclassified from a billing pull instead of a live
        # visibility check, leading to an unnecessary self-hosted-runner build
        # for 6 actually-public repos. This section is the console-side fix.
        page_src = PAGE.read_text()
        assert "gha_by_repo" in page_src
        assert "public vs private" in page_src.lower()

    def test_reconciliation_loader_reads_prefix(self):
        # alpha-engine-config#2849 — month-close true-up loader.
        loader_src = (REPO_ROOT / "loaders" / "s3_loader.py").read_text()
        assert "def load_expense_reconciliation" in loader_src
        assert '"expenses/reconciliation/' in loader_src

    def test_page_renders_reconciliation_section(self):
        page_src = PAGE.read_text()
        assert "load_expense_reconciliation" in page_src
        assert "reconciliation_table_rows" in page_src
        assert "Reconciliation (prior months)" in page_src


class TestFormatting:
    def test_usd(self):
        assert usd(None) == "—"
        assert usd(1234.5) == "$1,234.50"

    def test_quota_str(self):
        assert quota_str(None) == "—"
        assert quota_str({"unit": "Actions minutes", "used": 1400,
                          "limit": 2000}) == "1,400/2,000 Actions minutes"
        assert quota_str({"unit": "GB data transfer", "used": 3,
                          "limit": None}) == "3/? GB data transfer"

    def test_pace_badge_precedence(self):
        assert pace_badge({"status": "error", "pace": "over"}) == "⚠️ error"
        assert pace_badge({"status": "not_configured"}) == "⚙️ not configured"
        assert pace_badge({"status": "ok", "pace": "over"}) == "🔴 over"
        assert pace_badge({"status": "ok", "pace": "under"}) == "🟢 under"
        assert pace_badge({"status": "fixed", "pace": "fixed"}) == "💳 fixed"
        assert pace_badge({"status": "ok", "pace": None}) == "—"


class TestTableRows:
    def test_spend_first_ordering_and_shape(self):
        rows = provider_table_rows(DOC)
        assert [r["Provider"] for r in rows[:2]] == [
            "Claude Max 20x subscription", "AWS"]
        # No-MTD rows (error / not-configured) sink below $0 rows.
        assert {rows[-1]["Provider"], rows[-2]["Provider"]} == {
            "DeepSeek", "Neon Postgres"}
        aws = next(r for r in rows if r["Provider"] == "AWS")
        assert aws["MTD"] == "$12.34"
        assert aws["Pace"] == "🟢 under"
        gh = next(r for r in rows if "GitHub" in r["Provider"])
        assert gh["Quota"] == "1,400/2,000 Actions minutes"
        assert gh["Pace"] == "🔴 over"
        ds = next(r for r in rows if r["Provider"] == "DeepSeek")
        assert ds["Note"] == "HTTP 500 from api.deepseek.com"
        assert "_mtd_sort" not in rows[0]

    def test_error_rows(self):
        assert [p["key"] for p in error_rows(DOC)] == ["deepseek"]


class TestReconciliationTableRows:
    def test_worst_drift_first_and_shape(self):
        rows = reconciliation_table_rows(RECON_DOC)
        assert rows[0]["Provider"] == "aws"  # |37.5%| > |1.8%| > not-available(-1)
        assert rows[0]["Actual (final)"] == "$55.00"
        assert rows[0]["Delta $"] == "$15.00"
        assert "_abs_delta_pct" not in rows[0]

    def test_flagged_rows_get_warning_marker(self):
        rows = reconciliation_table_rows(RECON_DOC)
        aws = next(r for r in rows if r["Provider"] == "aws")
        openrouter = next(r for r in rows if r["Provider"] == "openrouter")
        assert aws["Delta %"].startswith("⚠️")  # 37.5% > 8% threshold
        assert not openrouter["Delta %"].startswith("⚠️")  # 1.8% < 8% threshold

    def test_not_available_row_renders_status_and_note(self):
        rows = reconciliation_table_rows(RECON_DOC)
        neon = next(r for r in rows if r["Provider"] == "neon")
        assert neon["Status"] == "— not available"
        assert neon["Delta %"] == "—"
        assert "current consumption period" in neon["Note"]

    def test_threshold_constant_matches_producer_convention(self):
        # Kept in lockstep with the collector's RECONCILIATION_DELTA_PCT_THRESHOLD
        # (alpha-engine-data expense-collector/index.py) — see module docstring.
        assert RECONCILIATION_DELTA_PCT_THRESHOLD == 0.08


class TestStaleness:
    def test_age_hours(self):
        now = datetime(2026, 7, 18, 0, 15, tzinfo=timezone.utc)
        assert as_of_age_hours(DOC, now=now) == 12.0

    def test_unparseable_as_of_returns_none(self):
        assert as_of_age_hours({"as_of": "not-a-date"}) is None
        assert as_of_age_hours({}) is None
