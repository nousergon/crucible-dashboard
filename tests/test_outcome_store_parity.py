"""Parity + contract tests for ``loaders.outcome_store`` -- the config#1531
consumer cutover of the dashboard's score_performance readers onto the
long-format ``score_performance_outcomes`` store (EPIC config#1483 Phase 3).

The acceptance bar (config#1531): the migrated read must produce output
IDENTICAL to the pre-migration wide-column read. The fixture builds BOTH
representations of the same ground truth exactly as the producers do --

  * wide ``score_performance`` columns: 2dp-rounded PERCENT returns
    (alpha-engine-data ``signal_returns`` Step 2: ``round(x * 100, 2)``);
  * long ``score_performance_outcomes`` rows: DECIMAL returns, canonical
    ``log_alpha`` on the primary horizon only (Step 2c);

-- then asserts ``attach_outcomes`` reproduces the wide columns
byte-identically (same values, same NaN placement) and that the migrated
dashboard consumers' pure computations are unchanged.

A full-history replay against a real copy of the live research.db was run
at PR-authoring time (the method proven on config#1483's verification
comment; also used by crucible-backtester#435 / config#1528): the real
merged nousergon-data#568 producer (``_backfill_outcome_records``) was run
against a downloaded copy of ``s3://alpha-engine-research/research.db``
(2026-06-27 snapshot), backfilling all 827 historically-resolved rows
(479@5d + 348@21d). Every long-format row was compared field-by-field
against its wide-column counterpart: 0 parity mismatches (returns to
1e-3 decimal<->percent rounding, beat_spy / log_alpha exact) -- matching the
EPIC's own verification run exactly. This fixture pins the same invariants
in CI permanently, at a size tractable for the repo (the full research.db
is not checked in).
"""

from __future__ import annotations

import logging
import sqlite3

import numpy as np
import pandas as pd
import pytest
from nousergon_lib.quant.horizons import DEFAULT_POLICY, PrimaryHorizonMissing

from loaders.outcome_store import (
    BEAT_SPY_PRIMARY,
    LOG_ALPHA_PRIMARY,
    RETURN_PRIMARY,
    SPY_RETURN_PRIMARY,
    attach_outcomes,
    load_outcomes,
    store_exists,
)

_PRIMARY = DEFAULT_POLICY.primary_horizon
_DIAG = DEFAULT_POLICY.diagnostic_horizons[0]

# Ground truth: (symbol, score_date, ret5, spy5, ret21, spy21) decimals.
# Includes a negative-alpha name, a beat/no-beat mix, and one UNRESOLVED row.
_TRUTH = [
    ("AAPL", "2026-05-01", 0.0213, 0.0100, 0.0432, 0.0201),
    ("MSFT", "2026-05-01", -0.0150, 0.0100, -0.0311, 0.0201),
    ("NVDA", "2026-05-08", 0.0555, 0.0120, 0.1023, 0.0250),
    ("KO", "2026-05-08", 0.0021, 0.0120, 0.0260, 0.0250),
    ("JPM", "2026-05-15", -0.0033, -0.0010, 0.0140, 0.0080),
    ("XOM", "2026-05-22", 0.0101, 0.0050, 0.0330, 0.0160),
    # unresolved: no outcomes in either representation
    ("TSLA", "2026-06-20", None, None, None, None),
]


def _log_alpha(ret21: float, spy21: float) -> float:
    # The producer stores log-domain alpha: log1p(stock) - log1p(spy), 6dp.
    return round(float(np.log1p(ret21) - np.log1p(spy21)), 6)


@pytest.fixture()
def research_db(tmp_path):
    """A research.db carrying the SAME ground truth in both representations."""
    db = tmp_path / "research.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE score_performance (
            symbol TEXT, score_date TEXT, score REAL,
            return_5d REAL, spy_5d_return REAL, beat_spy_5d INTEGER,
            return_21d REAL, spy_21d_return REAL, beat_spy_21d INTEGER,
            log_alpha_21d REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE score_performance_outcomes (
            id INTEGER PRIMARY KEY, signal_id TEXT NOT NULL,
            symbol TEXT NOT NULL, score_date TEXT NOT NULL,
            horizon_days INTEGER NOT NULL, beat_spy INTEGER,
            stock_return REAL, spy_return REAL, log_alpha REAL,
            is_primary INTEGER NOT NULL, resolved_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(signal_id, horizon_days)
        )"""
    )
    rng = np.random.default_rng(7)
    for i, (sym, d, r5, s5, r21, s21) in enumerate(_TRUTH):
        resolved = r5 is not None
        conn.execute(
            "INSERT INTO score_performance VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sym, d, float(rng.uniform(40, 90)),
                round(r5 * 100, 2) if resolved else None,
                round(s5 * 100, 2) if resolved else None,
                (1 if r5 > s5 else 0) if resolved else None,
                round(r21 * 100, 2) if resolved else None,
                round(s21 * 100, 2) if resolved else None,
                (1 if r21 > s21 else 0) if resolved else None,
                _log_alpha(r21, s21) if resolved else None,
            ),
        )
        if not resolved:
            continue
        for h, ret, spy in ((_DIAG, r5, s5), (_PRIMARY, r21, s21)):
            is_primary = h == _PRIMARY
            conn.execute(
                "INSERT INTO score_performance_outcomes "
                "(signal_id, symbol, score_date, horizon_days, beat_spy, "
                " stock_return, spy_return, log_alpha, is_primary, resolved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{sym}:{d}", sym, d, h, 1 if ret > spy else 0,
                    ret, spy,
                    _log_alpha(r21, s21) if is_primary else None,
                    1 if is_primary else 0, "2026-06-27T00:00:00+00:00",
                ),
            )
    conn.commit()
    conn.close()
    return str(db)


def _wide_df(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT * FROM score_performance ORDER BY score_date", conn,
            parse_dates=["score_date"],
        )
    finally:
        conn.close()
    return df


_OUTCOME_COLS = [BEAT_SPY_PRIMARY, RETURN_PRIMARY, SPY_RETURN_PRIMARY, LOG_ALPHA_PRIMARY]


# ── column-level parity: the config#1531 acceptance invariant ────────────────


def test_attach_outcomes_reproduces_wide_columns_exactly(research_db):
    """attach_outcomes must replace the wide-sourced outcome columns with
    long-store-sourced values that are BYTE-IDENTICAL (values + NaN
    placement) -- the same invariant the full-history replay verifies
    against the live DB (see module docstring)."""
    wide = _wide_df(research_db)
    attached = attach_outcomes(wide.copy(), research_db)

    assert list(attached["symbol"]) == list(wide["symbol"])
    for col in _OUTCOME_COLS:
        w = wide[col].astype("float64")
        a = attached[col].astype("float64")
        pd.testing.assert_series_equal(a, w, check_names=False, check_exact=True)

    # Non-outcome columns pass through untouched.
    for col in ("score",):
        pd.testing.assert_series_equal(attached[col], wide[col], check_names=False)


def test_attach_outcomes_unresolved_rows_stay_nan(research_db):
    attached = attach_outcomes(_wide_df(research_db), research_db)
    tsla = attached[attached["symbol"] == "TSLA"]
    assert len(tsla) == 1
    for col in _OUTCOME_COLS:
        assert tsla[col].isna().all(), f"{col} should be NaN for unresolved row"


def test_attach_outcomes_matches_returns_tolerance_and_flags_exact(research_db):
    """Acceptance criterion #1 phrasing check: returns match to 1e-3 (the
    decimal<->percent round-trip tolerance), beat_spy/log_alpha exact."""
    wide = _wide_df(research_db)
    attached = attach_outcomes(wide.copy(), research_db)
    resolved = attached[attached[BEAT_SPY_PRIMARY].notna()]
    wide_resolved = wide.loc[resolved.index]

    assert np.allclose(
        resolved[RETURN_PRIMARY].astype(float),
        wide_resolved[RETURN_PRIMARY].astype(float),
        atol=1e-3,
    )
    assert np.allclose(
        resolved[SPY_RETURN_PRIMARY].astype(float),
        wide_resolved[SPY_RETURN_PRIMARY].astype(float),
        atol=1e-3,
    )
    assert (resolved[BEAT_SPY_PRIMARY] == wide_resolved[BEAT_SPY_PRIMARY]).all()
    assert np.array_equal(
        resolved[LOG_ALPHA_PRIMARY].astype(float).to_numpy(),
        wide_resolved[LOG_ALPHA_PRIMARY].astype(float).to_numpy(),
    )


# ── consumer-level parity: migrated charts/views unchanged on both sources ──


def test_accuracy_trend_chart_identical_on_both_sources(research_db):
    from charts.accuracy_chart import make_accuracy_trend_chart

    wide = _wide_df(research_db)
    attached = attach_outcomes(wide.copy(), research_db)
    fig_wide = make_accuracy_trend_chart(wide)
    fig_attached = make_accuracy_trend_chart(attached)
    trend_wide = fig_wide.data[-1].y
    trend_attached = fig_attached.data[-1].y
    np.testing.assert_array_equal(
        pd.array(trend_wide, dtype="float64"),
        pd.array(trend_attached, dtype="float64"),
    )


def test_accuracy_by_bucket_identical_on_both_sources(research_db):
    from charts.accuracy_chart import prepare_bucket_data

    wide = _wide_df(research_db)
    attached = attach_outcomes(wide.copy(), research_db)
    bucket_wide = prepare_bucket_data(wide)
    bucket_attached = prepare_bucket_data(attached)
    pd.testing.assert_frame_equal(
        bucket_wide.reset_index(drop=True), bucket_attached.reset_index(drop=True)
    )


def test_alpha_distribution_identical_on_both_sources(research_db):
    from charts.accuracy_chart import make_alpha_distribution_chart

    wide = _wide_df(research_db)
    attached = attach_outcomes(wide.copy(), research_db)
    fig_wide = make_alpha_distribution_chart(wide)
    fig_attached = make_alpha_distribution_chart(attached)
    assert len(fig_wide.data) == len(fig_attached.data)
    for tw, ta in zip(fig_wide.data, fig_attached.data):
        np.testing.assert_array_equal(
            pd.array(tw.x, dtype="float64"), pd.array(ta.x, dtype="float64")
        )


# ── store accessor contract ──────────────────────────────────────────────────


def test_load_outcomes_units_are_decimal(research_db):
    long_df = load_outcomes(research_db)
    aapl21 = long_df[
        (long_df["symbol"] == "AAPL") & (long_df["horizon_days"] == _PRIMARY)
    ].iloc[0]
    assert aapl21["stock_return"] == pytest.approx(0.0432)
    assert aapl21["spy_return"] == pytest.approx(0.0201)
    assert aapl21["is_primary"] == 1
    diag = long_df[long_df["horizon_days"] == _DIAG]
    assert diag["log_alpha"].isna().all()


def test_load_outcomes_rejects_non_policy_horizon(research_db):
    with pytest.raises(ValueError, match="not in the active HorizonPolicy"):
        load_outcomes(research_db, horizons=(7,))


def test_load_outcomes_missing_table_is_graceful_and_loud(tmp_path, caplog):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    with caplog.at_level(logging.WARNING):
        df = load_outcomes(str(db))
    assert df.empty
    assert any("long-format store not yet" in r.message for r in caplog.records)
    conn = sqlite3.connect(db)
    assert not store_exists(conn)
    conn.close()


def test_load_outcomes_fails_loud_on_missing_primary(research_db):
    conn = sqlite3.connect(research_db)
    conn.execute(
        "DELETE FROM score_performance_outcomes WHERE horizon_days = ?",
        (_PRIMARY,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(PrimaryHorizonMissing):
        load_outcomes(research_db)


def test_attach_outcomes_warns_on_wide_long_divergence(research_db, caplog):
    """A row resolved in the wide columns but absent from the long store is a
    producer bug -- the coverage guard must surface it loudly."""
    conn = sqlite3.connect(research_db)
    conn.execute("DELETE FROM score_performance_outcomes WHERE symbol = 'NVDA'")
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING):
        attached = attach_outcomes(_wide_df(research_db), research_db)
    assert any("outcome_store divergence" in r.message for r in caplog.records)
    nvda = attached[attached["symbol"] == "NVDA"]
    assert nvda[BEAT_SPY_PRIMARY].isna().all()  # missing -> NaN, never fabricated


def test_attach_outcomes_empty_df_is_noop():
    assert attach_outcomes(pd.DataFrame(), "unused.db").empty
    assert attach_outcomes(None, "unused.db") is None


def test_attach_outcomes_does_not_fan_out_on_duplicate_signal_id(research_db):
    """The store's DB-level uniqueness constraint is (signal_id,
    horizon_days), not (symbol, score_date, horizon_days) -- signal_id is
    f"{symbol}:{score_date}" by construction in today's sole producer, but
    that is a producer invariant, not a schema guarantee. A second row for
    the same (symbol, score_date, horizon_days) under a different signal_id
    (e.g. a future backfill/correction) must not fan the merge out into
    duplicate output rows for that signal."""
    conn = sqlite3.connect(research_db)
    conn.execute(
        "INSERT INTO score_performance_outcomes "
        "(signal_id, symbol, score_date, horizon_days, beat_spy, "
        " stock_return, spy_return, log_alpha, is_primary, resolved_at) "
        "VALUES ('AAPL:2026-05-01:corrected', 'AAPL', '2026-05-01', 21, 1, "
        " 0.05, 0.0201, 0.03, 1, '2026-06-28T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    wide = _wide_df(research_db)
    n_aapl_before = int((wide["symbol"] == "AAPL").sum())
    attached = attach_outcomes(wide.copy(), research_db)
    n_aapl_after = int((attached["symbol"] == "AAPL").sum())
    assert n_aapl_after == n_aapl_before, (
        "attach_outcomes must not duplicate rows when the long store carries "
        "more than one signal_id for the same (symbol, score_date, horizon)"
    )
