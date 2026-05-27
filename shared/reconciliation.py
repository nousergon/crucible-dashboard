"""Portfolio reconciliation row builder for the order-book rationale view.

Pure data transform — joins per-ticker optimizer weights with the
optimizer's would_be_trades list to produce a target-vs-current-vs-
planned reconciliation per ticker. Consumed by
``pages/16_Order_Book_Rationale.py``. Extracted to ``shared/`` so the
math is unit-testable without importing the Streamlit page module.

Source fields are produced by ``alpha-engine`` schema 1.1.0
(``portfolio_nav`` + ``optimizer_trades`` + ``rebalance_band_pct`` on
the rationale payload). Pre-1.1.0 artifacts surface NAV=None, which the
caller renders as an empty-state caption.
"""
from __future__ import annotations

from typing import Any


# Row statuses — separate the optimizer's intentional "no-trade inside
# the rebalance band" (operationally a real decision, not a gap) from
# the "no-trade despite |Δ| ≥ band" case (would be surprising — surface
# for investigation).
STATUS_IN_BAND = "in_band"
STATUS_WOULD_TRADE = "would_trade"
STATUS_GAP_NO_TRADE = "gap_no_trade"

STATUS_LABEL = {
    STATUS_IN_BAND: "In band (intentional no-trade)",
    STATUS_WOULD_TRADE: "Would trade",
    STATUS_GAP_NO_TRADE: "Gap, no trade",
}


def _working_dollars_by_ticker(
    open_orders_payload: dict[str, Any] | None,
) -> dict[str, float]:
    """Compute working-order dollar exposure per ticker.

    Returns ``{ticker: signed_dollars}`` where the sign is BUY=positive,
    SELL=negative. Only orders flagged ``is_working`` count — terminal
    (Filled / Cancelled) orders are excluded so a just-filled order
    doesn't double-count against the optimizer plan.

    Pricing model:
      * Limit / stop orders contribute ``remaining * (limit_price or
        aux_price)``.
      * Market orders without a limit are excluded ($0) — they fill
        ~instantly so their working-state contribution is essentially
        zero in steady state.
    """
    if not open_orders_payload:
        return {}
    out: dict[str, float] = {}
    for rec in open_orders_payload.get("open_orders", []) or []:
        if not isinstance(rec, dict) or not rec.get("is_working"):
            continue
        ticker = rec.get("ticker")
        if not ticker:
            continue
        remaining = rec.get("remaining") or 0
        if remaining <= 0:
            continue
        price = rec.get("limit_price")
        if not isinstance(price, (int, float)) or price <= 0:
            price = rec.get("aux_price")
        if not isinstance(price, (int, float)) or price <= 0:
            # Unpriced (market order) — skip the $ but the row still
            # counts as working at the order-count level; that detail
            # is preserved in the producer's n_working summary.
            continue
        sign = 1.0 if rec.get("action") == "BUY" else -1.0
        out[ticker] = out.get(ticker, 0.0) + sign * float(remaining) * float(price)
    return out


def build_reconciliation_rows(
    payload: dict[str, Any],
    *,
    state_label: dict[str, str] | None = None,
    open_orders_payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build per-ticker reconciliation rows + a portfolio summary.

    Returns ``(rows, summary)``. ``rows`` is sorted by absolute Δ$
    descending so the highest-impact rebalances surface first.
    ``summary["nav"]`` is None when the artifact predates schema 1.1.0
    or comes from a legacy non-optimizer run (no shadow log), in which
    case ``rows`` is empty.

    Args:
        payload: a single order-book-rationale artifact (schema ≥ 1.1.0
            to populate; older artifacts produce an empty result).
        state_label: optional terminal-state → display-label map for
            the row's ``State`` column. Falls back to the raw state
            slug when not provided.
        open_orders_payload: optional ``trades/open_orders/latest.json``
            payload from the daemon. When present, each row carries a
            ``Working $`` column (signed: BUY positive / SELL negative)
            and the ``Residual $`` column nets out planned + working
            from Δ$. Tickers with no working orders show 0.
    """
    nav = payload.get("portfolio_nav")
    band = payload.get("rebalance_band_pct")
    trades = payload.get("optimizer_trades") or []
    trades_by_ticker = {
        t.get("ticker"): t for t in trades if isinstance(t, dict) and t.get("ticker")
    }
    working_by_ticker = _working_dollars_by_ticker(open_orders_payload)
    has_working_data = open_orders_payload is not None
    state_label = state_label or {}

    rows: list[dict[str, Any]] = []
    total_turnover = 0.0
    n_in_band = 0
    n_would_trade = 0
    n_gap_no_trade = 0

    for r in payload.get("tickers", []) or []:
        opt = r.get("optimizer") or {}
        cur_w = opt.get("current_weight")
        tgt_w = opt.get("target_weight")
        # Skip tickers without optimizer weights — nothing to compare.
        if cur_w is None or tgt_w is None:
            continue

        delta_w = tgt_w - cur_w
        delta_d = delta_w * nav if nav is not None else None
        trade = trades_by_ticker.get(r.get("ticker"))
        planned_d: float = 0.0
        if trade is not None:
            _pd = trade.get("delta_dollars")
            if isinstance(_pd, (int, float)):
                planned_d = float(_pd)
        working_d = working_by_ticker.get(r.get("ticker"), 0.0)
        # Residual = the gap still untouched after both planned and
        # working orders are netted out. Surfaces what's left for the
        # daemon to act on (or, in steady state, drift the operator
        # should know about).
        residual_d = (
            delta_d - planned_d - working_d if delta_d is not None else None
        )

        if band is not None and abs(delta_w) < band and trade is None:
            status = STATUS_IN_BAND
            n_in_band += 1
        elif trade is not None:
            status = STATUS_WOULD_TRADE
            n_would_trade += 1
            total_turnover += abs(planned_d)
        else:
            status = STATUS_GAP_NO_TRADE
            n_gap_no_trade += 1

        row: dict[str, Any] = {
            "Ticker": r.get("ticker"),
            "Held": "✓" if r.get("held") else "",
            "State": state_label.get(
                r.get("terminal_state"), r.get("terminal_state")
            ),
            "Cur %": cur_w * 100,
            "Tgt %": tgt_w * 100,
            "Δ %": delta_w * 100,
            "Δ $": delta_d,
            "Planned $": planned_d if trade is not None else None,
        }
        if has_working_data:
            row["Working $"] = working_d
        row["Residual $"] = residual_d
        row["Status"] = STATUS_LABEL[status]
        row["_status_raw"] = status
        row["_abs_delta_d"] = abs(delta_d) if delta_d is not None else 0.0
        rows.append(row)

    rows.sort(key=lambda row: row["_abs_delta_d"], reverse=True)

    total_working = sum(abs(v) for v in working_by_ticker.values())
    summary: dict[str, Any] = {
        "nav": nav if nav is not None else None,
        "band_pct": band,
        "n_tickers": len(rows),
        "n_in_band": n_in_band,
        "n_would_trade": n_would_trade,
        "n_gap_no_trade": n_gap_no_trade,
        "total_turnover": total_turnover,
        "total_working": total_working if has_working_data else None,
        "n_working_tickers": (
            sum(1 for v in working_by_ticker.values() if v != 0)
            if has_working_data else None
        ),
    }
    # Strip rows if NAV is missing — the dollar columns are meaningless
    # without it, and the caller renders the empty-state caption.
    if nav is None:
        return [], summary
    return rows, summary
