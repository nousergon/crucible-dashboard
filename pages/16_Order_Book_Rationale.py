"""
Order Book Rationale — Alpha Engine (private console)

Per-ticker daily decision record answering "why is ticker X in state S
today?" for the **whole considered universe** — including the tickers
that did NOT enter (excluded / vetoed are as important as approved).

Producer: alpha-engine executor ``order_book_rationale`` write at
morning-planner finalize (alpha-engine #189), canonical
``eval_artifacts`` shape at ``trades/order_book_rationale/``. This page
is the consumer-facing surface for ROADMAP Observability Item 4. It is
also the template instance of the shared ``artifact_archive`` component
that the per-process archive pages (Item 5) reuse.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from components.artifact_archive import ArchiveEntry, render_artifact_archive
from loaders.s3_loader import (
    load_open_orders_latest,
    load_order_book_rationale_history,
)
from shared.reconciliation import (
    STATUS_GAP_NO_TRADE,
    STATUS_IN_BAND,
    STATUS_WOULD_TRADE,
    build_reconciliation_rows,
)


st.set_page_config(
    page_title="Order Book Rationale — Alpha Engine",
    page_icon="📋",
    layout="wide",
)


st.divider()

# State → display color (mirrors the executor terminal-state vocab).
# Held uses a saturated teal — operators must be able to spot
# currently-held positions at a glance, distinct from the muted
# no_action default.
_STATE_COLOR = {
    "approved_entry": "#1b5e20",      # green — entering
    "urgent_exit": "#b71c1c",         # red — exiting
    "reduce": "#e65100",              # orange — trimming
    "predictor_vetoed": "#4a148c",    # purple — ML veto
    "risk_blocked": "#880e4f",        # magenta — hard risk gate
    "held": "#004d40",                # teal — currently held in portfolio
    "no_action": "#1e1e1e",           # neutral — pre-1.2.0 aggregate slug
    # 1.2.0+ sub-states — research HOLD/EXIT/REDUCE on non-held tickers
    # is filtered at the producer (dead signal — no order possible), so
    # the only sub-states reachable here are the optimizer-driven ones.
    "no_action_optimizer_zero_weight": "#1a237e",  # indigo
    "no_action_unknown": "#5d4037",                # muted-red — bug-flag
}

_STATE_LABEL = {
    "approved_entry": "Approved entry",
    "urgent_exit": "Urgent exit",
    "reduce": "Reduce",
    "predictor_vetoed": "Predictor vetoed",
    "risk_blocked": "Risk blocked",
    "held": "Held",
    "no_action": "No action",
    # 1.2.0+ sub-states — labels phrase the operator-readable reason so
    # the table answers "why?" without requiring the drill-down.
    "no_action_optimizer_zero_weight": "No action — optimizer chose 0",
    "no_action_unknown": "No action — unknown (investigate)",
}

_RECON_STATUS_COLOR = {
    STATUS_IN_BAND: "#1e1e1e",
    STATUS_WOULD_TRADE: "#1b5e20",
    STATUS_GAP_NO_TRADE: "#b71c1c",
}


def _chain_str(chain: list[dict]) -> str:
    """Compact one-line decision chain: stage:result → stage:result."""
    parts = []
    for s in chain or []:
        seg = f"{s.get('stage')}:{s.get('result')}"
        if s.get("rule"):
            seg += f"({s['rule']})"
        parts.append(seg)
    return "  →  ".join(parts)


def _render_reconciliation(payload: dict) -> None:
    """Render the target-vs-current-vs-planned reconciliation section.

    Sourced from the producer's schema-1.1.0 portfolio_nav +
    optimizer_trades + rebalance_band_pct fields. Gracefully no-ops
    with an explanatory caption on pre-1.1.0 artifacts.
    """
    # Open-IB-orders snapshot is daemon-published each tick; absent
    # before the producer ships (alpha-engine #223) or outside trading
    # hours. Tolerated as None — the table just omits Working $.
    open_orders_payload = load_open_orders_latest()
    rows, summary = build_reconciliation_rows(
        payload,
        state_label=_STATE_LABEL,
        open_orders_payload=open_orders_payload,
    )
    st.markdown("##### Portfolio reconciliation — target vs current vs planned")
    if not rows or summary["nav"] is None:
        st.caption(
            "Reconciliation view requires optimizer fields (schema ≥ 1.1.0). "
            "This artifact is either pre-deploy or from a legacy "
            "non-optimizer run — re-runs after the producer ships will "
            "populate it."
        )
        return

    nav = summary["nav"]
    band_pct = summary["band_pct"]
    cols = st.columns(5)
    cols[0].metric("NAV", f"${nav:,.0f}")
    cols[1].metric(
        "Would trade",
        summary["n_would_trade"],
        help="Tickers the optimizer wants to rebalance (|Δ| ≥ band).",
    )
    cols[2].metric(
        "In band",
        summary["n_in_band"],
        help=(
            "Tickers inside the rebalance band — optimizer's "
            "intentional no-trade decision."
        ),
    )
    cols[3].metric(
        "Gap, no trade",
        summary["n_gap_no_trade"],
        help=(
            "Tickers with |Δ| ≥ band but no planned trade — should "
            "be 0; non-zero is worth investigating."
        ),
    )
    cols[4].metric(
        "Turnover ($)",
        f"${summary['total_turnover']:,.0f}",
        help="Sum of |planned $| across would-trade rows.",
    )
    if band_pct is not None:
        # Caption phrasing depends on whether daemon snapshot is live.
        # When open_orders_payload is None the Working $ column is
        # omitted and Residual = Δ - Planned (legacy reconciliation).
        if summary.get("total_working") is not None:
            st.caption(
                f"Rebalance band: **{band_pct * 100:.2f}%** of NAV "
                f"(`{band_pct * nav:,.0f}` USD).  ·  "
                f"Working $ (live from IB Gateway): "
                f"**${summary['total_working']:,.0f}** across "
                f"{summary['n_working_tickers']} ticker(s). "
                f"Residual $ = Δ$ − Planned $ − Working $ (gap still untouched)."
            )
        else:
            st.caption(
                f"Rebalance band: **{band_pct * 100:.2f}%** of NAV "
                f"(`{band_pct * nav:,.0f}` USD).  ·  "
                "Working-orders snapshot not available — "
                "Residual $ = Δ$ − Planned $."
            )

    df = pd.DataFrame(rows)
    display_df = df.drop(columns=["_status_raw", "_abs_delta_d"])

    def _row_color(display_row):
        status = df.at[display_row.name, "_status_raw"]
        color = _RECON_STATUS_COLOR.get(status, "#1e1e1e")
        return [f"background-color: {color}; color: #fff"] * len(display_row)

    styled = display_df.style.apply(_row_color, axis=1).format(
        {
            "Cur %": "{:.2f}%",
            "Tgt %": "{:.2f}%",
            "Δ %": "{:+.2f}%",
            "Δ $": "${:+,.0f}",
            "Planned $": "${:+,.0f}",
            "Working $": "${:+,.0f}",
            "Residual $": "${:+,.0f}",
        },
        na_rep="—",
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _exclusion_str(exc: dict | None) -> str:
    if not exc:
        return ""
    rule = exc.get("rule") or "—"
    reason = exc.get("reason") or ""
    val, thr = exc.get("value"), exc.get("threshold")
    bound = f" [{val} vs {thr}]" if val is not None and thr is not None else ""
    return f"{rule}{bound}: {reason}".strip()


def _render_rationale(payload: dict) -> None:
    """Render one rationale artifact. Expander-free (it is itself shown
    inside a history expander) — drill-down via a selectbox, not nested
    expanders, per the artifact_archive contract."""
    if not isinstance(payload, dict) or not payload.get("tickers"):
        st.info("Artifact present but no tickers in the considered universe.")
        return

    summary = payload.get("summary", {})
    # Currently-held count is the truth column the user asks for first.
    # Sourced from the per-record `held` boolean (portfolio truth via
    # current_positions, alpha-engine #201). Falls back to the
    # n_held summary key for pre-#201 artifacts.
    n_held_now = sum(1 for r in payload["tickers"] if r.get("held"))
    if n_held_now == 0:
        n_held_now = summary.get("n_held", 0)
    meta_cols = st.columns(5)
    meta_cols[0].metric("Considered", summary.get("n_considered", 0))
    meta_cols[1].metric("Currently held", n_held_now)
    meta_cols[2].metric(
        "Entries",
        summary.get("n_approved_entry", 0),
    )
    meta_cols[3].metric(
        "Exits / reduces",
        summary.get("n_urgent_exit", 0) + summary.get("n_reduce", 0),
    )
    meta_cols[4].metric(
        "Blocked / vetoed",
        summary.get("n_risk_blocked", 0) + summary.get("n_predictor_vetoed", 0),
    )
    st.caption(
        f"market_regime: **{payload.get('market_regime', '—')}**  ·  "
        f"signal_date: {payload.get('signal_date', '—')}  ·  "
        f"prediction_date: {payload.get('prediction_date', '—')}  ·  "
        f"run_id: `{payload.get('run_id', '—')}`"
    )

    _render_reconciliation(payload)
    st.markdown("##### Per-ticker decision chain")

    rows = []
    for r in payload["tickers"]:
        research = r.get("research") or {}
        pred = r.get("predictor") or {}
        opt = r.get("optimizer") or {}
        cur_w = opt.get("current_weight")
        tgt_w = opt.get("target_weight")
        rows.append({
            "Ticker": r.get("ticker"),
            "Held": "✓" if r.get("held") else "",
            "State": _STATE_LABEL.get(r.get("terminal_state"), r.get("terminal_state")),
            "_state_raw": r.get("terminal_state"),
            "Cur wt": cur_w * 100 if cur_w is not None else None,
            "Tgt wt": tgt_w * 100 if tgt_w is not None else None,
            "Research": research.get("signal"),
            "Score": research.get("score"),
            "Conviction": research.get("conviction"),
            "Sector rating": research.get("sector_rating"),
            "Predictor": pred.get("predicted_direction"),
            "Conf": pred.get("prediction_confidence"),
            "Exclusion": _exclusion_str(r.get("exclusion")),
            "Decision chain": _chain_str(r.get("decision_chain")),
        })
    df = pd.DataFrame(rows)
    display_df = df.drop(columns=["_state_raw"])

    def _row_color(display_row):
        state = df.at[display_row.name, "_state_raw"]
        color = _STATE_COLOR.get(state, "#1e1e1e")
        return [f"background-color: {color}; color: #fff"] * len(display_row)

    styled = display_df.style.apply(_row_color, axis=1).format(
        {"Score": "{:.1f}", "Conf": "{:.2f}",
         "Cur wt": "{:.2f}%", "Tgt wt": "{:.2f}%"},
        na_rep="—",
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Per-ticker full decision chain (selectbox — NOT an expander, so
    # this renderer is safe inside the archive's history expanders).
    tickers = [r.get("ticker") for r in payload["tickers"]]
    sel = st.selectbox(
        "Inspect full decision chain",
        ["—"] + tickers,
        key=f"obr_sel_{payload.get('run_id')}",
    )
    if sel and sel != "—":
        rec = next((r for r in payload["tickers"] if r.get("ticker") == sel), None)
        if rec:
            st.json(rec)


def _label(payload: dict) -> str:
    td = payload.get("trading_day") or payload.get("calendar_date") or "?"
    return f"{td}"


def _summary_caption(payload: dict) -> str:
    s = payload.get("summary", {})
    n_held = sum(1 for r in payload.get("tickers", []) if r.get("held"))
    if n_held == 0:
        n_held = s.get("n_held", 0)
    return (
        f"{s.get('n_considered', 0)} considered · "
        f"{n_held} held · "
        f"{s.get('n_approved_entry', 0)} entries · "
        f"{s.get('n_urgent_exit', 0) + s.get('n_reduce', 0)} exits/reduces · "
        f"{s.get('n_risk_blocked', 0) + s.get('n_predictor_vetoed', 0)} blocked/vetoed"
    )


history = load_order_book_rationale_history(n_recent=14)
entries = [
    ArchiveEntry(
        label=_label(p),
        sort_key=str(p.get("run_id") or p.get("trading_day") or ""),
        payload=p,
        summary=_summary_caption(p),
    )
    for p in (history or [])
]

render_artifact_archive(
    title="Order Book Rationale",
    description=(
        "Per-ticker decision chain for the whole considered universe — "
        "why each ticker is in its order-book state today, including the "
        "ones that did not enter. Latest trading day rendered inline; "
        "prior ~2 weeks one click each. Producer: executor "
        "order_book_rationale (alpha-engine #189)."
    ),
    entries=entries,
    render_fn=_render_rationale,
    retention_days=14,
    empty_message=(
        "No order-book rationale artifacts yet — this populates after the "
        "executor morning planner next runs post-deploy "
        "(s3://alpha-engine-research/trades/order_book_rationale/)."
    ),
)

