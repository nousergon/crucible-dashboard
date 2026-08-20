"""Tests for the "Last Write" staleness column on views/Data_and_Maturity.py
(config-I2638): a research.db table's row COUNT must not render identically
whether its producer is healthy or has been frozen for weeks.

Uses the importlib-from-file + stubbed-loaders pattern established by
tests/test_ticker_detail.py (which itself mirrors test_s3_loader.py) so the
view module executes fully offline -- no S3, no research.db, no config.yaml.
"""

import importlib.util
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

_VIEWS = Path(__file__).parent.parent / "views"


def _load_data_and_maturity(table_counts: dict, table_max_dates: dict):
    """Load views/Data_and_Maturity.py with every loader import stubbed.

    ``table_counts``: {table_name: row_count} fed through a fake sqlite
    connection so ``_table_counts()`` returns exactly these numbers.
    ``table_max_dates``: returned verbatim by the stubbed
    ``get_table_max_dates()`` -- this is the value under test.
    """
    fake_conn = MagicMock()

    def fake_execute(sql, *a, **kw):
        cur = MagicMock()
        for table, count in table_counts.items():
            if f"FROM {table}" in sql:
                cur.fetchone.return_value = (count,)
                return cur
        cur.fetchone.return_value = (0,)
        return cur

    fake_conn.execute.side_effect = fake_execute

    cache_stub = types.ModuleType("loaders.cache")
    cache_stub.cached = lambda **kw: (lambda f: f)

    db_loader_stub = types.ModuleType("loaders.db_loader")
    db_loader_stub.load_research_db = lambda: fake_conn
    db_loader_stub.get_table_max_dates = lambda *a, **kw: dict(table_max_dates)

    outcome_store_stub = types.ModuleType("loaders.outcome_store")
    outcome_store_stub.load_outcomes = lambda conn, horizons=(21,): []

    s3_loader_stub = types.ModuleType("loaders.s3_loader")
    s3_loader_stub._fetch_s3_json = lambda *a, **kw: None
    s3_loader_stub._research_bucket = lambda: "test-research"
    s3_loader_stub._trades_bucket = lambda: "test-trades"
    s3_loader_stub.get_s3_client = lambda: MagicMock()
    s3_loader_stub.list_s3_prefixes = lambda *a, **kw: []
    s3_loader_stub.load_eod_pnl = lambda: None
    s3_loader_stub.load_trades_full = lambda: None

    pkg = types.ModuleType("loaders")
    pkg.cache = cache_stub
    pkg.db_loader = db_loader_stub
    pkg.outcome_store = outcome_store_stub
    pkg.s3_loader = s3_loader_stub

    mod_names = (
        "loaders", "loaders.cache", "loaders.db_loader",
        "loaders.outcome_store", "loaders.s3_loader",
    )
    saved = {k: sys.modules.get(k) for k in mod_names}
    sys.modules["loaders"] = pkg
    sys.modules["loaders.cache"] = cache_stub
    sys.modules["loaders.db_loader"] = db_loader_stub
    sys.modules["loaders.outcome_store"] = outcome_store_stub
    sys.modules["loaders.s3_loader"] = s3_loader_stub
    try:
        spec = importlib.util.spec_from_file_location(
            f"data_and_maturity_{id(fake_conn)}", str(_VIEWS / "Data_and_Maturity.py")
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


def _row(module, label):
    dataset, records, last_write = (
        module.volume_data["Dataset"],
        module.volume_data["Records"],
        module.volume_data["Last Write"],
    )
    i = dataset.index(label)
    return records[i], last_write[i]


# ── Pure helpers ────────────────────────────────────────────────────────────


def test_age_days():
    mod = _load_data_and_maturity({}, {})
    assert mod._age_days(date.today().isoformat()) == 0
    old = (date.today() - timedelta(days=40)).isoformat()
    assert mod._age_days(old) == 40
    assert mod._age_days(None) is None
    assert mod._age_days("garbage") is None


def test_format_age_fresh_vs_frozen_vs_unknown():
    mod = _load_data_and_maturity({}, {})
    fresh = date.today().isoformat()
    frozen = (date.today() - timedelta(days=41)).isoformat()
    assert "FROZEN" not in mod._format_age(fresh)
    assert "ago)" in mod._format_age(fresh)
    assert "FROZEN" in mod._format_age(frozen)
    assert frozen in mod._format_age(frozen)
    assert mod._format_age(None) == "—"


def test_format_age_boundary_is_frozen():
    # Exactly the threshold (14d) counts as frozen -- inclusive boundary.
    mod = _load_data_and_maturity({}, {})
    boundary = (date.today() - timedelta(days=14)).isoformat()
    assert "FROZEN" in mod._format_age(boundary)


# ── Full view: frozen table distinguishable from healthy one ───────────────


def test_frozen_table_flagged_healthy_table_is_not():
    fresh = date.today().isoformat()
    frozen = (date.today() - timedelta(days=41)).isoformat()
    # investment_thesis and cio_evaluations both have similar row counts
    # (the exact regression named in config-I2638's measured facts) but
    # different producer health.
    mod = _load_data_and_maturity(
        table_counts={"investment_thesis": 500, "cio_evaluations": 480},
        table_max_dates={"investment_thesis": frozen, "cio_evaluations": fresh},
    )
    thesis_records, thesis_lw = _row(mod, "Signals (investment_thesis)")
    cio_records, cio_lw = _row(mod, "CIO Evaluations (eval)")

    assert thesis_records == 500
    assert cio_records == 480
    # Same shape of count, but staleness renders them differently -- this
    # is the whole point of the column.
    assert "FROZEN" in thesis_lw
    assert "FROZEN" not in cio_lw
    assert thesis_lw != cio_lw


def test_table_inputs_row_present_and_unknown_last_write_is_dash():
    # team_inputs (config-I2638) previously wasn't even counted on this
    # page; it must now appear with an explicit "—" when no measured write
    # date is available (never a manufactured date).
    mod = _load_data_and_maturity(
        table_counts={"team_inputs": 0},
        table_max_dates={"team_inputs": None},
    )
    assert "Team Inputs (eval)" in mod.volume_data["Dataset"]
    records, last_write = _row(mod, "Team Inputs (eval)")
    assert records == 0
    assert last_write == "—"


def test_s3_count_rows_have_no_last_write_column_value():
    # Trades/EOD/signal-dates rows aren't research.db tables -- they carry
    # their own freshness surfaces (Fleet Status / Artifact Freshness) and
    # must not silently print a fabricated "—" that reads as "checked, no
    # data" when nobody wired a check at all -- they're honestly "—" too,
    # but for a different, explicit reason. Assert every row still names a
    # value (no KeyError/IndexError across the three parallel lists).
    mod = _load_data_and_maturity({}, {})
    assert len(mod.volume_data["Dataset"]) == len(mod.volume_data["Records"])
    assert len(mod.volume_data["Dataset"]) == len(mod.volume_data["Last Write"])
    records, last_write = _row(mod, "Trades (executed)")
    assert last_write == "—"
