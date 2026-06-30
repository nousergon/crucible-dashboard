"""Pure transforms for the Universe Board page (no Streamlit) — so the
cross-repo consumer contract with crucible-research ``scoring/universe_board.py``
(``scanner/universe/latest.json``, ``schema_version=2``) is unit-testable
independently of the Streamlit chrome in ``views/39_Universe_Board.py``.

schema_version=2 (SOTA attractiveness): ``attractiveness_score`` is now the
cross-sectional percentile of a sector-neutral z-blend; new fields
``attractiveness_raw``, ``pillar_contributions``, ``gate_stage``, ``gate_trace``
and top-level ``pillar_weights`` / ``gate_config`` power the WHY/HOW detail
panel. All consumed defensively — a schema_version=1 artifact (no v2 fields)
still flattens (the new columns degrade to None / empty).
"""
from __future__ import annotations

import pandas as pd

PILLARS = ["quality", "value", "momentum", "growth", "stewardship", "defensiveness"]

# Display labels for the metric columns (used by the page's filters + table).
METRIC_LABELS = {
    "pe": "P/E", "pb": "P/B", "div_yield": "Dividend yield", "fcf_yield": "FCF yield",
    "debt_to_equity": "Debt/Equity", "current_ratio": "Current ratio", "payout": "Payout ratio",
    "roe": "ROE", "gross_margin": "Gross margin", "rev_gr_3y": "Revenue growth 3y",
    "eps_gr_3y": "EPS growth 3y", "mkt_cap": "Market cap", "rsi": "RSI(14)",
    "mom_20d": "Momentum 20d", "ret_60d": "Return 60d", "ret_120d": "Return 120d",
    "vol_20d": "Realized vol 20d", "atr_pct": "ATR %", "beta": "Beta",
    "dist_52w_hi": "Dist from 52w high", "vs_ma200": "Price vs MA200", "avg_vol": "Avg volume",
    "tech": "Tech score", "focus": "Focus score",
    "tradeability": "Tradeability", "expected_cost_bps": "Round-trip cost (bps)",
}

# Columns that stay textual (never coerced to numeric).
_TEXT_COLS = ("ticker", "sector", "country", "industry", "stance", "gate",
              "fail_reason", "gate_stage")

# Ordered funnel stages → friendly labels for the gate_stage column / trace.
GATE_STAGE_LABELS = {
    "passed": "✅ Passed", "no_data": "No data", "liquidity": "Liquidity",
    "price_floor": "Price floor", "volatility": "Volatility",
    "below_thresholds": "Below thresholds", "rank_cutoff": "Rank cutoff",
}

# metric-block key in the artifact → display column in the DataFrame.
_METRIC_MAP = {
    "current_price": "price", "market_cap": "mkt_cap", "pe": "pe", "pb": "pb",
    "fcf_yield": "fcf_yield", "dividend_yield": "div_yield", "debt_to_equity": "debt_to_equity",
    "current_ratio": "current_ratio", "payout_ratio": "payout", "roe": "roe",
    "gross_margin": "gross_margin", "revenue_growth_3y": "rev_gr_3y", "eps_growth_3y": "eps_gr_3y",
    "rsi_14": "rsi", "momentum_20d": "mom_20d", "return_60d": "ret_60d", "return_120d": "ret_120d",
    "realized_vol_20d": "vol_20d", "atr_pct": "atr_pct", "dist_from_52w_high": "dist_52w_hi",
    "price_vs_ma200": "vs_ma200", "beta": "beta", "avg_volume": "avg_vol",
}


def flatten_stock(stock: dict) -> dict:
    """One universe-board stock record → a flat display row. Missing fields
    degrade to None (a coverage gap, never a guessed value)."""
    pillars = stock.get("pillars", {}) or {}
    metrics = stock.get("metrics", {}) or {}
    gate = stock.get("gate", {}) or {}
    # Tradeability (schema_version=3): an INDEPENDENT cost-to-access score, shown
    # alongside attractiveness but NEVER blended with it (ARCHITECTURE §43).
    # Degrades to None on a v2/v1 artifact (no tradeability block).
    tradeability = stock.get("tradeability") or {}
    row = {
        "ticker": stock.get("ticker"),
        "sector": stock.get("sector") or "Unknown",
        "country": stock.get("country") or "Unknown",
        "industry": stock.get("industry"),
        "attractiveness": stock.get("attractiveness_score"),
        "attractiveness_raw": stock.get("attractiveness_raw"),  # v2; None on v1
        "tradeability": tradeability.get("tradeability_score"),       # v3; None earlier
        "expected_cost_bps": tradeability.get("expected_cost_bps"),   # v3; None earlier
    }
    for p in PILLARS:
        row[p] = pillars.get(p)
    row["focus"] = stock.get("focus_score")
    row["stance"] = stock.get("focus_stance")
    row["tech"] = stock.get("tech_score")
    row["gate"] = "PASS" if int(gate.get("quant_filter_pass", 0) or 0) == 1 else "FAIL"
    row["fail_reason"] = gate.get("filter_fail_reason")
    row["gate_stage"] = stock.get("gate_stage")  # v2; None on v1
    for src, dst in _METRIC_MAP.items():
        row[dst] = metrics.get(src)
    return row


def flatten_board(board: dict) -> pd.DataFrame:
    """The board artifact → a numeric-coerced display DataFrame. Empty board →
    empty frame (the page graceful-degrades to its explainer)."""
    stocks = (board or {}).get("stocks") or []
    df = pd.DataFrame([flatten_stock(s) for s in stocks])
    if df.empty:
        return df
    for col in df.columns:
        if col not in _TEXT_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ── schema_version=2 detail-panel transforms (the WHY / HOW surfaces) ─────────

def board_meta(board: dict) -> dict:
    """Top-level board metadata for the page header + gates expander. Defaults
    keep a v1 artifact rendering (no pillar_weights / gate_config)."""
    board = board or {}
    return {
        "schema_version": board.get("schema_version"),
        "as_of": board.get("as_of"),
        "attractiveness_method": board.get("attractiveness_method", ""),
        "pillar_weights": board.get("pillar_weights") or {},
        "gate_config": board.get("gate_config") or {},
    }


def index_by_ticker(board: dict) -> dict[str, dict]:
    """``{ticker: stock_record}`` for the detail-panel lookup."""
    return {s.get("ticker"): s for s in (board or {}).get("stocks") or [] if s.get("ticker")}


def contributions_df(stock: dict) -> pd.DataFrame:
    """Per-pillar additive contributions (w·z/Σw, signed) for the selected
    stock, ordered by the pillar order — the WHY of its attractiveness. Empty
    frame when absent (v1 artifact or a no-pillar name)."""
    contribs = (stock or {}).get("pillar_contributions") or {}
    rows = [{"pillar": p, "contribution": contribs[p]} for p in PILLARS if p in contribs]
    return pd.DataFrame(rows, columns=["pillar", "contribution"])


def gate_trace_df(stock: dict) -> pd.DataFrame:
    """The per-stock funnel trace (each gate: value vs threshold, pass/fail) as
    a tidy frame for display — the HOW of the scanner's verdict. Empty frame on
    a v1 artifact (no gate_trace)."""
    trace = (stock or {}).get("gate_trace") or []
    rows = []
    for g in trace:
        passed = g.get("pass")
        rows.append({
            "stage": g.get("stage"),
            "metric": g.get("metric"),
            "value": g.get("value"),
            "op": g.get("op"),
            "threshold": g.get("threshold"),
            "result": "—" if passed is None else ("pass" if passed else "FAIL"),
        })
    return pd.DataFrame(rows, columns=["stage", "metric", "value", "op", "threshold", "result"])
