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
from shared.target_weights import build_target_weight_matrix




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
    # ERROR — optimizer assigned a non-zero target but no order was created
    # (allocation dropped downstream, e.g. an IBKR price-resolve failure).
    # Bright red so it reads as a fault, not a benign no-action.
    "no_action_optimizer_dropped": "#d50000",      # bright red — ERROR
    "no_action_unknown": "#5d4037",                # muted-red — bug-flag
}

# Terminal states that are genuine producer faults — surfaced as a page-top
# error banner so the operator cannot miss them (not just a colored row).
_ERROR_STATES = ("no_action_optimizer_dropped", "no_action_unknown")

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
    "no_action_optimizer_dropped": "⚠ ERROR — optimizer targeted, order dropped",
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


def _render_book_status_banner(payload: dict) -> None:
    """Top-of-page one-line "why did/didn't the book move today" status.

    Reads the schema-1.3.0 ``book_status`` field (producer:
    crucible-executor#311). Absent on pre-1.3.0 artifacts → render nothing
    (the per-ticker ERROR banner below still covers the dropped-allocation
    case for older artifacts), so this page is safe ahead of the producer
    merge.
    """
    bs = payload.get("book_status")
    if not isinstance(bs, dict):
        return
    state = bs.get("state")
    headline = bs.get("headline") or state or "—"
    renderer = {
        "allocations_dropped": st.error,
        "hold_book_safeguard": st.warning,
        "rebalanced": st.success,
        "no_rebalance_at_target": st.info,
    }.get(state, st.info)
    renderer(f"**{headline}**")

    # Dispersion sub-line — what made a low-conviction day low-conviction.
    disp = bs.get("dispersion") or {}
    bits: list[str] = []
    if disp.get("n_predictions"):
        bits.append(f"{disp['n_predictions']} predictions")
    a_std = disp.get("alpha_stdev")
    if isinstance(a_std, (int, float)):
        bits.append(f"α σ={a_std:.4f}")
    nu, nd, nf = disp.get("n_up"), disp.get("n_down"), disp.get("n_flat")
    if nu is not None and nd is not None:
        skew = f"{nu}↑/{nd}↓"
        if nf:
            skew += f"/{nf}→"
        bits.append(skew)
    if disp.get("signal_degenerate"):
        bits.append("⚠ tradable signal degenerate")
    to = bs.get("turnover_one_way")
    band = bs.get("rebalance_band_pct")
    if isinstance(to, (int, float)):
        seg = f"one-way turnover {to * 100:.2f}%"
        if isinstance(band, (int, float)):
            band_pct = band * 100 if band <= 1 else band
            seg += f" (band {band_pct:.1f}%)"
        bits.append(seg)
    if bits:
        st.caption(" · ".join(bits))


def _render_rationale(payload: dict) -> None:
    """Render one rationale artifact. Expander-free (it is itself shown
    inside a history expander) — drill-down via a selectbox, not nested
    expanders, per the artifact_archive contract."""
    if not isinstance(payload, dict) or not payload.get("tickers"):
        st.info("Artifact present but no tickers in the considered universe.")
        return

    # Daily HOLD-vs-fault status banner (schema 1.3.0) — first thing the
    # operator sees: did the book move today, and if not, why (benign HOLD
    # vs hold-book safeguard vs dropped allocation).
    _render_book_status_banner(payload)

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

    # ── ERROR banner — producer faults the operator must not miss ───────────
    # A non-zero optimizer target that never became an order
    # (no_action_optimizer_dropped) means the allocation was LOST downstream
    # (e.g. an IBKR price-resolve failure). Surface it as a page-top error,
    # not just a red row, so it's caught even when the table is collapsed.
    _dropped = [
        r.get("ticker")
        for r in payload["tickers"]
        if r.get("terminal_state") == "no_action_optimizer_dropped"
    ]
    _unknown = [
        r.get("ticker")
        for r in payload["tickers"]
        if r.get("terminal_state") == "no_action_unknown"
    ]
    if _dropped:
        st.error(
            f"⚠ **{len(_dropped)} optimizer allocation(s) DROPPED** — the "
            f"optimizer targeted a non-zero weight but no order was created "
            f"for: **{', '.join(_dropped)}**. The allocation was lost "
            f"(likely a price-resolve failure). Check the executor log + the "
            f"`AlphaEngine/Executor/optimizer_target_dropped` CloudWatch alarm."
        )
    if _unknown:
        st.warning(
            f"{len(_unknown)} ticker(s) in **unknown** no-action state "
            f"(investigate): {', '.join(_unknown)}."
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
    # Forward-looking intraday artifact: headline the day the order book is
    # FOR — calendar_date (today, even pre-close, since it executes today) —
    # NOT the backward-looking trading_day. trading_day is the last-closed
    # session whose signals/predictions fed the book; it stays as input
    # provenance (shown secondary), never the headline. Predictive/intraday
    # operator surfaces use current day; realized artifacts (eval, EOD,
    # backtest) keep trading_day. See alpha-engine-docs DATE_CONVENTIONS.md.
    cd = payload.get("calendar_date")
    td = payload.get("trading_day")
    if cd and td and cd != td:
        return f"{cd} (signals {td})"
    return str(cd or td or "?")


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


def _tw_cell_styles(df: pd.DataFrame) -> pd.DataFrame:
    """Per-cell green-intensity background scaled to target % (cap 15%).

    Manual CSS styler (no matplotlib ``background_gradient`` dependency —
    matplotlib is not a dashboard dep). NaN cells get no background so
    "—" reads as absent, not as a 0% target. Interpolates from the page's
    neutral ``#1e1e1e`` to the approved-entry teal-green ``#1b5e20``.
    """
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in df.columns:
        for idx in df.index:
            v = df.at[idx, col]
            if v is None or pd.isna(v):
                continue
            frac = max(0.0, min(float(v) / 15.0, 1.0))
            r = round(0x1E + (0x1B - 0x1E) * frac)
            g = round(0x1E + (0x5E - 0x1E) * frac)
            b = round(0x1E + (0x20 - 0x1E) * frac)
            styles.at[idx, col] = f"background-color: #{r:02x}{g:02x}{b:02x}; color: #fff"
    return styles


def _render_target_weight_timeseries(history: list[dict]) -> None:
    """Cross-day matrix of optimizer holdings targets — target % per calendar
    day. Read-only: pivots the ``optimizer.target_weight`` already carried
    in each loaded artifact (no producer change). Columns are keyed on the
    calendar date the book is FOR (forward-looking surface), not the
    backward-looking trading_day. The actual/forming book by default;
    toggle to the full considered universe."""
    st.markdown("##### Target-weight evolution — optimizer holdings targets by calendar day")
    usable = [p for p in (history or []) if isinstance(p, dict) and p.get("tickers")]
    if len(usable) < 2:
        st.caption(
            "Target-weight history needs ≥2 sessions of order-book "
            "rationale artifacts — this fills in as executor runs accumulate."
        )
        return

    full_universe = st.toggle(
        "Show full considered universe",
        value=False,
        key="obr_tw_full_universe",
        help=(
            "Off: held positions + approved entries only (the actual / "
            "forming book). On: every ticker the optimizer assigned a "
            "target on any day in the window."
        ),
    )
    df = build_target_weight_matrix(usable, held_only=not full_universe)
    if df.empty:
        st.caption("No optimizer target weights in the loaded window.")
        return

    full_days = list(df.columns)
    # Narrow column labels (MM-DD); the full window is stated in the caption.
    df = df.rename(columns={d: d[5:] if len(d) >= 10 else d for d in full_days})
    st.caption(
        f"{len(df)} ticker(s) × {len(df.columns)} session(s) "
        f"({full_days[0]} → {full_days[-1]}). Cells are target % of NAV; "
        "**—** = no optimizer target that day (not in the universe), "
        "distinct from a deliberate 0.00%. Rows sorted by latest-day target."
    )
    styled = df.style.format("{:.2f}%", na_rep="—").apply(_tw_cell_styles, axis=None)
    st.dataframe(styled, use_container_width=True)

    with st.expander("Line chart", expanded=False):
        # Transpose → index = calendar day, one line per ticker. NaN gaps
        # break the line where a ticker left the universe.
        st.line_chart(df.T)


history = load_order_book_rationale_history(n_recent=14)

_render_target_weight_timeseries(history)
st.divider()

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

