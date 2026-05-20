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
from loaders.s3_loader import load_order_book_rationale_history


st.set_page_config(
    page_title="Order Book Rationale — Alpha Engine",
    page_icon="📋",
    layout="wide",
)


st.divider()

# State → display color (mirrors the executor terminal-state vocab).
_STATE_COLOR = {
    "approved_entry": "#1b5e20",      # green — entering
    "urgent_exit": "#b71c1c",         # red — exiting
    "reduce": "#e65100",              # orange — trimming
    "predictor_vetoed": "#4a148c",    # purple — ML veto
    "risk_blocked": "#880e4f",        # magenta — hard risk gate
    "held": "#37474f",                # slate — no change
    "no_action": "#263238",           # dark — considered, untouched
}

_STATE_LABEL = {
    "approved_entry": "Approved entry",
    "urgent_exit": "Urgent exit",
    "reduce": "Reduce",
    "predictor_vetoed": "Predictor vetoed",
    "risk_blocked": "Risk blocked",
    "held": "Held",
    "no_action": "No action",
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
    meta_cols = st.columns(4)
    meta_cols[0].metric("Considered", summary.get("n_considered", 0))
    meta_cols[1].metric(
        "Entries",
        summary.get("n_approved_entry", 0),
    )
    meta_cols[2].metric(
        "Exits / reduces",
        summary.get("n_urgent_exit", 0) + summary.get("n_reduce", 0),
    )
    meta_cols[3].metric(
        "Blocked / vetoed",
        summary.get("n_risk_blocked", 0) + summary.get("n_predictor_vetoed", 0),
    )
    st.caption(
        f"market_regime: **{payload.get('market_regime', '—')}**  ·  "
        f"signal_date: {payload.get('signal_date', '—')}  ·  "
        f"prediction_date: {payload.get('prediction_date', '—')}  ·  "
        f"run_id: `{payload.get('run_id', '—')}`"
    )

    rows = []
    for r in payload["tickers"]:
        research = r.get("research") or {}
        pred = r.get("predictor") or {}
        rows.append({
            "Ticker": r.get("ticker"),
            "State": _STATE_LABEL.get(r.get("terminal_state"), r.get("terminal_state")),
            "_state_raw": r.get("terminal_state"),
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
        color = _STATE_COLOR.get(state, "#263238")
        return [f"background-color: {color}; color: #fff"] * len(display_row)

    styled = display_df.style.apply(_row_color, axis=1).format(
        {"Score": "{:.1f}", "Conf": "{:.2f}"}, na_rep="—"
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
    return (
        f"{s.get('n_considered', 0)} considered · "
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

