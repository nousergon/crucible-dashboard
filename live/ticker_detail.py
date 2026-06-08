"""Per-ticker detail modal for live.nousergon.ai Holdings & Trades (ROADMAP L176).

`show_ticker_detail(ticker, positions_snapshot, trades_df)` is an `st.dialog`
that joins everything the system already knows about one ticker onto a single
pop-out: current holding state, the rolling rationale, the latest persisted
thesis archive, today's predictor read, research composite + sub-scores +
price-target upside, recent fills, and the OBR decision block if the ticker
was touched today.

Placed as a bare `live/` module (mirrors `live/shared.py`) rather than
`live/components/` on purpose: the live pages import shared widgets from the
TOP-LEVEL `components/` package (uptime_kpi, report_card), and `live/` is
first on sys.path — so a `live/components/` package would SHADOW the top-level
one and break those imports. A bare module sidesteps that and lets
`from loaders.s3_loader import ...` resolve to `live/loaders` correctly.

Every lookup is defensive + lazily fetched (TTL-cached at the loader); a
missing source renders an explicit "not available" line rather than an empty
panel (feedback_no_silent_fails — never a silent empty modal).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    load_company_names,
    load_latest_signals,
    load_live_day_return,
    load_order_book_rationale,
    load_predictions_json,
    load_universe_archive,
)


def _signals_entry(ticker: str) -> dict:
    """The latest signals.json universe entry for this ticker (or {})."""
    sig = load_latest_signals() or {}
    for entry in sig.get("universe") or []:
        if isinstance(entry, dict) and entry.get("ticker") == ticker:
            return entry
    return {}


def _obr_block(ticker: str) -> dict | None:
    """The OBR `considered` decision block for this ticker, if present today."""
    obr = load_order_book_rationale() or {}
    for rec in obr.get("considered") or []:
        if isinstance(rec, dict) and rec.get("ticker") == ticker:
            return rec
    return None


def _fmt_pct(v) -> str:
    """Format a FRACTION as a percent (0.025 → '+2.5%'). Use for fields stored
    as decimals — price_target_upside, predicted_alpha."""
    try:
        return f"{float(v) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct_points(v) -> str:
    """Format a value ALREADY in percent points (2.5 → '+2.5%').

    `eod_reconcile` writes `daily_return_pct` and `alpha_contribution_pct`
    pre-scaled by ×100 (e.g. `(price/prior - 1) * 100`). Running those through
    `_fmt_pct` double-scaled them (a +2.5% day rendered as '+250.0%') — this
    formatter is the correct one for the snapshot's `*_pct` fields."""
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _company_name(ticker: str, pos: dict, sig: dict) -> str | None:
    """Best-effort full company name: position/signals first (free), then the
    SEC ticker→name map (TTL-cached). Falls back to None → bare ticker."""
    return pos.get("name") or sig.get("name") or load_company_names().get(ticker.upper())


def _position_info(ticker: str, positions_snapshot) -> dict:
    """Pull this ticker's row out of the EOD positions_snapshot (dict or list)."""
    if isinstance(positions_snapshot, dict):
        info = positions_snapshot.get(ticker)
        return info if isinstance(info, dict) else {}
    if isinstance(positions_snapshot, list):
        for p in positions_snapshot:
            if isinstance(p, dict) and p.get("ticker") == ticker:
                return p
    return {}


def _render(ticker: str, positions_snapshot, trades_df: "pd.DataFrame | None") -> None:
    # CASH (or any operator-reserved pseudo-ticker) — no underlying, no modal body.
    if ticker == "CASH":
        st.markdown("### 💵 CASH")
        st.info("Operator-reserved cash — no underlying security.")
        return

    pos = _position_info(ticker, positions_snapshot)
    sig = _signals_entry(ticker)
    archive = load_universe_archive(ticker) or {}
    pred = (load_predictions_json() or {}).get(ticker) or {}

    # ── Header ───────────────────────────────────────────────────────────
    name = _company_name(ticker, pos, sig)
    sector = pos.get("sector") or sig.get("sector") or "—"
    header = f"### {ticker}" + (f" — {name}" if name else "")
    st.markdown(header)
    st.caption(f"Sector: {sector}")

    held = bool(pos)
    if not held:
        # Surface the last exit for a traded-but-not-held ticker.
        st.caption("Not currently held.")

    # ── Current state (held positions) ───────────────────────────────────
    if held:
        c1, c2, c3 = st.columns(3)
        mv = pos.get("market_value")
        c1.metric("Market value", f"${mv:,.0f}" if isinstance(mv, (int, float)) else "—")
        c1.metric("Shares", f"{pos.get('shares', '—')}")
        c2.metric("Avg cost", f"${pos.get('avg_cost'):,.2f}" if isinstance(pos.get("avg_cost"), (int, float)) else "—")
        upnl = pos.get("unrealized_pnl")
        c2.metric("Unrealized P&L", f"${upnl:,.0f}" if isinstance(upnl, (int, float)) else "—")
        # daily_return_pct / alpha_contribution_pct are already in percent points
        # (eod_reconcile pre-scales by ×100) — format WITHOUT a second ×100.
        #
        # Day return: prefer a live 15-min-delayed quote (today's % change vs
        # prior close) so the modal reflects TODAY, not the last EOD snapshot
        # (which lags a full session until tonight's reconcile). Falls back to
        # the snapshot's stored daily_return_pct when the quote is unavailable.
        live_dr = load_live_day_return(ticker)
        is_live = live_dr is not None
        day_return = live_dr if is_live else pos.get("daily_return_pct")
        c3.metric(
            "Day return" + (" (live)" if is_live else ""),
            _fmt_pct_points(day_return),
            help=(
                "Live 15-min-delayed quote — today's % change vs prior close."
                if is_live
                else "Last closed session (from the EOD snapshot) — live quote unavailable."
            ),
        )
        # Alpha contrib stays the EOD attribution figure (portfolio-weighted
        # excess vs SPY) — an inherently end-of-session number, not live.
        c3.metric("Alpha contrib", _fmt_pct_points(pos.get("alpha_contribution_pct")))

    # ── Rationale ────────────────────────────────────────────────────────
    st.markdown("#### Rationale")
    summary = sig.get("thesis_summary")
    if summary:
        st.write(summary)
    if archive:
        when = archive.get("date") or archive.get("last_material_change_date")
        if when:
            st.caption(f"Latest persisted thesis: {when}")
        cat = archive.get("key_catalyst")
        risk = archive.get("key_risk")
        if cat:
            st.markdown(f"**Catalyst:** {cat}")
        if risk:
            st.markdown(f"**Key risk:** {risk}")
        changes = archive.get("material_changes")
        if changes:
            st.markdown(f"**Recent material change:** {changes}")
    elif not summary:
        st.info("No persisted thesis archive yet — see the latest cycle summary on the main page.")

    # ── Research read ────────────────────────────────────────────────────
    if sig:
        st.markdown("#### Research")
        rc1, rc2, rc3 = st.columns(3)
        score = sig.get("score", archive.get("research_score"))
        rc1.metric("Composite score", f"{score:.0f}" if isinstance(score, (int, float)) else "—")
        rc2.metric("Rating", str(sig.get("rating") or archive.get("rating") or "—"))
        rc3.metric("Upside", _fmt_pct(sig.get("price_target_upside")))
        sub = sig.get("sub_scores")
        if isinstance(sub, dict) and sub:
            sub_df = pd.DataFrame(
                [{"Sub-score": k, "Value": v} for k, v in sub.items()]
            )
            st.dataframe(sub_df, hide_index=True, width="stretch")

    # ── Predictor read (today) ───────────────────────────────────────────
    if pred:
        st.markdown("#### Predictor (today)")
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("Direction", str(pred.get("predicted_direction") or pred.get("direction") or "—"))
        conf = pred.get("prediction_confidence", pred.get("confidence"))
        pc2.metric("Confidence", f"{conf:.0%}" if isinstance(conf, (int, float)) else "—")
        alpha = pred.get("predicted_alpha")
        pc3.metric("Predicted alpha", _fmt_pct(alpha) if alpha is not None else "—")

    # ── Recent fills for this ticker ─────────────────────────────────────
    if trades_df is not None and not trades_df.empty and "ticker" in trades_df.columns:
        fills = trades_df[trades_df["ticker"] == ticker]
        if not fills.empty:
            st.markdown("#### Recent fills")
            cols = [c for c in ("date", "action", "signal", "filled_shares", "shares", "fill_price", "price_at_order") if c in fills.columns]
            show = fills[cols].copy()
            if "date" in show.columns:
                show = show.sort_values("date", ascending=False).head(8)
            st.dataframe(show.reset_index(drop=True), hide_index=True, width="stretch")

    # ── OBR decision block (if the ticker was considered today) ──────────
    obr = _obr_block(ticker)
    if obr:
        st.markdown("#### Order-book decision (today)")
        decision = obr.get("decision") or obr.get("action") or obr.get("status")
        if decision:
            st.markdown(f"**Decision:** {decision}")
        reason = obr.get("reason") or obr.get("rationale") or obr.get("note")
        if reason:
            st.write(reason)


# st.dialog renders a true modal overlay (X / outside-click / Esc dismiss).
# Title is static at decoration; the dynamic per-ticker header is rendered
# inside _render so the same dialog serves every ticker without leaking
# state across rows (each invocation re-fetches lazily for its argument).
@st.dialog("Position detail", width="large")
def show_ticker_detail(ticker: str, positions_snapshot, trades_df=None) -> None:
    _render(ticker, positions_snapshot, trades_df)
