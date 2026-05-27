"""Unit tests for shared.reconciliation.build_reconciliation_rows.

Covers the target-vs-current-vs-planned reconciliation math + the
three-way status classification (in_band / would_trade / gap_no_trade)
that the order-book rationale page surfaces.
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.reconciliation import (  # noqa: E402
    STATUS_GAP_NO_TRADE,
    STATUS_IN_BAND,
    STATUS_WOULD_TRADE,
    build_reconciliation_rows,
)


def _record(
    ticker: str,
    *,
    cur_w: float | None = None,
    tgt_w: float | None = None,
    held: bool = False,
    state: str = "no_action",
) -> dict:
    opt = {}
    if cur_w is not None:
        opt["current_weight"] = cur_w
    if tgt_w is not None:
        opt["target_weight"] = tgt_w
    return {
        "ticker": ticker,
        "held": held,
        "terminal_state": state,
        "optimizer": opt,
    }


def _payload(records: list[dict], *, nav=125_000.0, band=0.005, trades=None) -> dict:
    return {
        "portfolio_nav": nav,
        "rebalance_band_pct": band,
        "optimizer_trades": trades or [],
        "tickers": records,
    }


# ── happy path ────────────────────────────────────────────────────────


def test_would_trade_row_carries_planned_and_residual():
    # AAPL: cur 0%, tgt 4.1%, optimizer plans full $5125 BUY → residual 0.
    payload = _payload(
        [_record("AAPL", cur_w=0.0, tgt_w=0.041)],
        trades=[{
            "ticker": "AAPL", "action": "BUY",
            "delta_weight": 0.041, "delta_dollars": 5125.0,
            "target_weight": 0.041, "current_weight": 0.0,
        }],
    )
    rows, summary = build_reconciliation_rows(payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["Ticker"] == "AAPL"
    assert r["_status_raw"] == STATUS_WOULD_TRADE
    assert r["Δ $"] == 5125.0
    assert r["Planned $"] == 5125.0
    assert r["Residual $"] == 0.0
    assert summary["n_would_trade"] == 1
    assert summary["total_turnover"] == 5125.0


def test_in_band_row_classified_as_intentional_no_trade():
    # KO: |Δ| = 0.2% < band 0.5%, no optimizer trade → in_band.
    payload = _payload([_record("KO", cur_w=0.030, tgt_w=0.032, held=True)])
    rows, summary = build_reconciliation_rows(payload)
    ko = next(r for r in rows if r["Ticker"] == "KO")
    assert ko["_status_raw"] == STATUS_IN_BAND
    assert ko["Planned $"] is None
    # Residual = full Δ$ (no planned trade)
    assert ko["Residual $"] == ko["Δ $"]
    assert summary["n_in_band"] == 1


def test_gap_no_trade_row_surfaces_for_investigation():
    # XOM: |Δ| = 2% ≥ band but absent from would_be_trades → gap_no_trade
    # (the operationally surprising case the user wanted surfaced).
    payload = _payload([_record("XOM", cur_w=0.020, tgt_w=0.040)])
    rows, summary = build_reconciliation_rows(payload)
    xom = next(r for r in rows if r["Ticker"] == "XOM")
    assert xom["_status_raw"] == STATUS_GAP_NO_TRADE
    assert xom["Planned $"] is None
    assert summary["n_gap_no_trade"] == 1


def test_residual_when_planned_partially_closes_gap():
    # Optimizer wants to move $4000 but only $3000 planned → $1000 residual.
    payload = _payload(
        [_record("MSFT", cur_w=0.0, tgt_w=0.032)],
        trades=[{
            "ticker": "MSFT", "action": "BUY",
            "delta_dollars": 3000.0,
            "target_weight": 0.032, "current_weight": 0.0,
        }],
    )
    rows, _ = build_reconciliation_rows(payload)
    msft = rows[0]
    assert msft["Δ $"] == 4000.0
    assert msft["Planned $"] == 3000.0
    assert msft["Residual $"] == 1000.0


def test_rows_sorted_by_absolute_delta_dollars_desc():
    payload = _payload([
        _record("A", cur_w=0.0, tgt_w=0.01),      # Δ$ 1250
        _record("B", cur_w=0.0, tgt_w=0.05),      # Δ$ 6250
        _record("C", cur_w=0.03, tgt_w=0.0),      # Δ$ -3750
    ])
    rows, _ = build_reconciliation_rows(payload)
    assert [r["Ticker"] for r in rows] == ["B", "C", "A"]


# ── filter & defaults ────────────────────────────────────────────────


def test_tickers_without_optimizer_weights_excluded():
    # A held ticker the optimizer didn't score has no weights → cannot
    # participate in the reconciliation, must be omitted.
    payload = _payload([
        _record("AAPL", cur_w=0.0, tgt_w=0.041),
        _record("MYSTERY"),  # no optimizer dict
    ])
    rows, summary = build_reconciliation_rows(payload, state_label={})
    assert {r["Ticker"] for r in rows} == {"AAPL"}
    assert summary["n_tickers"] == 1


def test_state_label_map_applied_to_state_column():
    payload = _payload(
        [_record("AAPL", cur_w=0.0, tgt_w=0.041, state="approved_entry")],
        trades=[{"ticker": "AAPL", "delta_dollars": 5125.0}],
    )
    rows, _ = build_reconciliation_rows(
        payload, state_label={"approved_entry": "Approved entry"}
    )
    assert rows[0]["State"] == "Approved entry"


def test_missing_state_label_falls_back_to_raw_slug():
    payload = _payload([_record("XOM", cur_w=0.0, tgt_w=0.01, state="weird")])
    rows, _ = build_reconciliation_rows(payload, state_label={})
    assert rows[0]["State"] == "weird"


# ── legacy / pre-deploy graceful degradation ─────────────────────────


def test_missing_nav_returns_empty_rows_with_summary_marker():
    # Pre-1.1.0 artifact — no portfolio_nav field. Caller must be able
    # to detect this and render the explanatory empty-state caption.
    payload = {
        "portfolio_nav": None,
        "rebalance_band_pct": None,
        "optimizer_trades": None,
        "tickers": [_record("AAPL", cur_w=0.0, tgt_w=0.041)],
    }
    rows, summary = build_reconciliation_rows(payload)
    assert rows == []
    assert summary["nav"] is None


def test_no_optimizer_trades_field_treated_as_empty():
    # If the producer omits optimizer_trades entirely (legacy or partial
    # shadow log) every ticker either lands in_band or gap_no_trade.
    payload = {
        "portfolio_nav": 100_000.0,
        "rebalance_band_pct": 0.005,
        "tickers": [_record("KO", cur_w=0.030, tgt_w=0.032)],
    }
    rows, _ = build_reconciliation_rows(payload)
    assert rows[0]["_status_raw"] == STATUS_IN_BAND


def test_missing_rebalance_band_falls_back_to_gap_no_trade():
    # Without a band threshold we can't call anything "intentional
    # no-trade" — every untraded gap must surface as gap_no_trade so it
    # doesn't silently disappear.
    payload = {
        "portfolio_nav": 100_000.0,
        "rebalance_band_pct": None,
        "optimizer_trades": [],
        "tickers": [_record("KO", cur_w=0.030, tgt_w=0.0301)],
    }
    rows, _ = build_reconciliation_rows(payload)
    assert rows[0]["_status_raw"] == STATUS_GAP_NO_TRADE


def test_trade_with_null_delta_dollars_does_not_crash():
    # Defensive: optimizer trade list with a None delta_dollars (e.g.
    # serialization quirk) must not crash the math.
    payload = _payload(
        [_record("AAPL", cur_w=0.0, tgt_w=0.041)],
        trades=[{"ticker": "AAPL", "delta_dollars": None}],
    )
    rows, _ = build_reconciliation_rows(payload)
    assert rows[0]["Planned $"] == 0.0


def test_empty_tickers_list_returns_empty_summary():
    payload = _payload([])
    rows, summary = build_reconciliation_rows(payload)
    assert rows == []
    assert summary["n_tickers"] == 0
    assert summary["total_turnover"] == 0.0


# ── Working $ column (daemon-side open-orders snapshot) ───────────────


def _open_order(
    ticker: str,
    action: str,
    *,
    remaining: int = 100,
    limit: float | None = 50.0,
    aux: float | None = None,
    is_working: bool = True,
) -> dict:
    return {
        "ticker": ticker,
        "action": action,
        "remaining": remaining,
        "limit_price": limit,
        "aux_price": aux,
        "is_working": is_working,
    }


def test_working_column_added_when_open_orders_payload_present():
    payload = _payload(
        [_record("AAPL", cur_w=0.0, tgt_w=0.041)],
        trades=[{"ticker": "AAPL", "delta_dollars": 5125.0}],
    )
    open_orders = {"open_orders": [_open_order("AAPL", "BUY", remaining=50, limit=100.0)]}
    rows, summary = build_reconciliation_rows(
        payload, open_orders_payload=open_orders,
    )
    r = rows[0]
    assert "Working $" in r
    assert r["Working $"] == 5000.0  # 50 shares × $100 limit
    # Residual = Δ$ - Planned $ - Working $ = 5125 - 5125 - 5000 = -5000
    assert r["Residual $"] == -5000.0
    assert summary["total_working"] == 5000.0
    assert summary["n_working_tickers"] == 1


def test_working_column_omitted_when_no_open_orders_payload():
    payload = _payload(
        [_record("AAPL", cur_w=0.0, tgt_w=0.041)],
        trades=[{"ticker": "AAPL", "delta_dollars": 5125.0}],
    )
    rows, summary = build_reconciliation_rows(payload)
    assert "Working $" not in rows[0]
    assert summary["total_working"] is None
    # Legacy residual math: Δ$ - Planned $ (no working subtraction)
    assert rows[0]["Residual $"] == 0.0


def test_sell_orders_sign_working_dollars_negative():
    payload = _payload([_record("MSFT", cur_w=0.032, tgt_w=0.0)])
    open_orders = {"open_orders": [
        _open_order("MSFT", "SELL", remaining=50, limit=80.0),
    ]}
    rows, _ = build_reconciliation_rows(payload, open_orders_payload=open_orders)
    msft = rows[0]
    assert msft["Working $"] == -4000.0
    # Δ$ = (0 - 0.032) × 125000 = -4000; Residual = -4000 - 0 - (-4000) = 0
    assert msft["Residual $"] == 0.0


def test_terminal_status_orders_excluded_from_working():
    # Filled / Cancelled orders show is_working=False — must not
    # contribute to Working $ even if they're in the snapshot.
    payload = _payload([_record("AAPL", cur_w=0.0, tgt_w=0.041)])
    open_orders = {"open_orders": [
        _open_order("AAPL", "BUY", remaining=100, limit=50.0, is_working=False),
    ]}
    rows, _ = build_reconciliation_rows(payload, open_orders_payload=open_orders)
    assert rows[0]["Working $"] == 0.0


def test_market_order_without_limit_excluded_from_working_dollars():
    # Market orders have limit_price=0; without a usable price the
    # working-$ contribution is 0 (they fill ~instantly anyway).
    payload = _payload([_record("AAPL", cur_w=0.0, tgt_w=0.041)])
    open_orders = {"open_orders": [
        _open_order("AAPL", "BUY", remaining=100, limit=0.0, aux=None),
    ]}
    rows, _ = build_reconciliation_rows(payload, open_orders_payload=open_orders)
    assert rows[0]["Working $"] == 0.0


def test_stop_order_uses_aux_price_when_no_limit():
    payload = _payload([_record("AAPL", cur_w=0.041, tgt_w=0.0)])
    open_orders = {"open_orders": [
        _open_order("AAPL", "SELL", remaining=100, limit=0.0, aux=45.0),
    ]}
    rows, _ = build_reconciliation_rows(payload, open_orders_payload=open_orders)
    assert rows[0]["Working $"] == -4500.0


def test_multiple_orders_for_same_ticker_summed():
    payload = _payload([_record("AAPL", cur_w=0.0, tgt_w=0.041)])
    open_orders = {"open_orders": [
        _open_order("AAPL", "BUY", remaining=30, limit=50.0),
        _open_order("AAPL", "BUY", remaining=20, limit=50.0),
    ]}
    rows, _ = build_reconciliation_rows(payload, open_orders_payload=open_orders)
    assert rows[0]["Working $"] == 2500.0  # 50 shares × $50


def test_empty_open_orders_payload_treated_as_no_working():
    payload = _payload(
        [_record("AAPL", cur_w=0.0, tgt_w=0.041)],
        trades=[{"ticker": "AAPL", "delta_dollars": 5125.0}],
    )
    rows, summary = build_reconciliation_rows(
        payload, open_orders_payload={"open_orders": []},
    )
    assert rows[0]["Working $"] == 0.0
    assert summary["total_working"] == 0.0
    assert summary["n_working_tickers"] == 0
