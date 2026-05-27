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


def build_reconciliation_rows(
    payload: dict[str, Any],
    *,
    state_label: dict[str, str] | None = None,
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
    """
    nav = payload.get("portfolio_nav")
    band = payload.get("rebalance_band_pct")
    trades = payload.get("optimizer_trades") or []
    trades_by_ticker = {
        t.get("ticker"): t for t in trades if isinstance(t, dict) and t.get("ticker")
    }
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
        residual_d = (
            delta_d - planned_d if delta_d is not None else None
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

        rows.append({
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
            "Residual $": residual_d,
            "Status": STATUS_LABEL[status],
            "_status_raw": status,
            "_abs_delta_d": abs(delta_d) if delta_d is not None else 0.0,
        })

    rows.sort(key=lambda row: row["_abs_delta_d"], reverse=True)

    summary: dict[str, Any] = {
        "nav": nav if nav is not None else None,
        "band_pct": band,
        "n_tickers": len(rows),
        "n_in_band": n_in_band,
        "n_would_trade": n_would_trade,
        "n_gap_no_trade": n_gap_no_trade,
        "total_turnover": total_turnover,
    }
    # Strip rows if NAV is missing — the dollar columns are meaningless
    # without it, and the caller renders the empty-state caption.
    if nav is None:
        return [], summary
    return rows, summary
