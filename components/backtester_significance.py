"""Console rendering for the backtester's observe-first significance verdicts
(config#1426 soak review surface / config#1444 items 1+2).

Reads ``metrics.json["significance_observe"]`` — a map
``{optimizer_key: observe_record}`` produced by the backtester's
`evaluate._collect_significance_observe`. Each record carries:
``gate, significant, would_block, did_promote, promotes_on_undefended_evidence,
enforced, detail``.

Pure helpers (unit-tested) + a thin Streamlit ``render`` at the bottom. The
verdict is OBSERVE-ONLY (never enforced) — the table makes the
would-promote-vs-did soak reviewable so Phase 4 can ratify the observe→enforce
flip.
"""

from __future__ import annotations

from typing import Any

# metrics.json optimizer key → human label (also fixes display order).
OPTIMIZER_LABELS: dict[str, str] = {
    "weight_result": "Scoring weights",
    "veto_result": "Predictor veto",
    "predictor_sizing": "Predictor sizing",
    "barrier_sizing": "Barrier sizing",
    "stance_sizing": "Stance sizing",
}

# detail.status values that mean "no real verdict was computable".
_NO_VERDICT_STATUSES = {
    "insufficient_data", "no_variance", "insufficient_stances",
    "insufficient_stances", "missing_column",
}


def _has_verdict(rec: dict) -> bool:
    """True when the record carries a genuine significance decision (not skipped
    for insufficient/absent data)."""
    detail = rec.get("detail")
    if isinstance(detail, dict):
        status = detail.get("status")
        if status in _NO_VERDICT_STATUSES:
            return False
    return True


def _num(v: Any, fmt: str = ".3f") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else "—"


def evidence_summary(detail: dict | None) -> str:
    """Compact, gate-shape-aware one-liner of the underlying evidence."""
    if not isinstance(detail, dict):
        return "—"
    method = detail.get("method")
    status = detail.get("status")
    # IC-based (predictor/barrier sizing) — has ic + bootstrap CI.
    if detail.get("ic") is not None and method != "two_sample_mean_diff_bootstrap":
        ci = (
            f", CI[{_num(detail.get('ci_low'))}, {_num(detail.get('ci_high'))}]"
            if detail.get("ci_low") is not None else ""
        )
        p = detail.get("p_value")
        pstr = f", p={_num(p)}" if isinstance(p, (int, float)) else ""
        return f"IC={_num(detail.get('ic'))}{ci}{pstr} (n={detail.get('n', '—')})"
    # Wilson lower-bound vs base rate (veto).
    if method == "wilson_lower_bound_vs_base_rate":
        return (
            f"precision={_num(detail.get('rate'))} vs base={_num(detail.get('base_rate'))}, "
            f"CI_low={_num(detail.get('ci_low'))} (n={detail.get('n', '—')})"
        )
    # Two-sample mean-diff bootstrap (stance spread).
    if method == "two_sample_mean_diff_bootstrap":
        return (
            f"Δμ={_num(detail.get('estimate'))}, "
            f"CI[{_num(detail.get('ci_low'))}, {_num(detail.get('ci_high'))}] "
            f"({detail.get('best_stance', '?')} vs {detail.get('worst_stance', '?')})"
        )
    # Weight optimizer — per-subscore significance map.
    if isinstance(detail.get("per_subscore"), dict):
        sig = [k for k, v in detail["per_subscore"].items() if v.get("significant")]
        return (
            f"significant sub-scores: {', '.join(sig) if sig else 'none'} "
            f"(n_test={detail.get('n_test', '—')})"
        )
    return str(status) if status else "—"


def significance_observe_rows(sig: dict | None) -> list[dict]:
    """Flatten the significance_observe block into display rows (stable order)."""
    rows: list[dict] = []
    for key, label in OPTIMIZER_LABELS.items():
        rec = (sig or {}).get(key)
        if not isinstance(rec, dict):
            continue
        if not _has_verdict(rec):
            significant = "n/a (insufficient)"
            would_block = "—"
        else:
            significant = "yes" if rec.get("significant") else "no"
            would_block = "yes" if rec.get("would_block") else "no"
        promoted = rec.get("did_promote")
        rows.append({
            "Optimizer": label,
            "Significant?": significant,
            "Would block": would_block,
            "Promoted (live)": "yes" if promoted else ("no" if promoted is not None else "—"),
            "⚠": "⚠ UNDEFENDED" if rec.get("promotes_on_undefended_evidence") else "",
            "Evidence": evidence_summary(rec.get("detail")),
        })
    return rows


def count_undefended(sig: dict | None) -> tuple[int, int]:
    """(# optimizers promoting on undefended evidence, # with a real verdict)."""
    undefended = 0
    with_verdict = 0
    for key in OPTIMIZER_LABELS:
        rec = (sig or {}).get(key)
        if not isinstance(rec, dict):
            continue
        if _has_verdict(rec):
            with_verdict += 1
        if rec.get("promotes_on_undefended_evidence"):
            undefended += 1
    return undefended, with_verdict


def render(metrics: dict | None) -> None:
    """Streamlit section. Safe to call with any/empty metrics."""
    import streamlit as st

    st.subheader("Promotion-Gate Significance (observe)")
    st.caption(
        "Observe-only (config#1426): does each auto-apply optimizer's promotion "
        "evidence clear a significance bar? **Not enforced** — soak surface for the "
        "Phase-4 observe→enforce ratification."
    )
    sig = (metrics or {}).get("significance_observe")
    if not sig:
        st.info(
            "No significance_observe block in this run's metrics.json "
            "(populated by backtester runs from 2026-07-04 onward)."
        )
        return

    undefended, with_verdict = count_undefended(sig)
    if undefended:
        st.warning(
            f"⚠ {undefended} of {with_verdict} optimizers with a verdict are "
            f"**promoting on statistically-undefended evidence** this run."
        )
    elif with_verdict:
        st.success(f"All {with_verdict} optimizers with a verdict cleared the significance bar.")

    rows = significance_observe_rows(sig)
    if rows:
        import pandas as pd
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ── Cross-date trend (config#1444 item 2) ────────────────────────────────────

# Top-level metrics.json keys carrying the headline series (verified live).
def trend_row(date_str: str, metrics: dict | None) -> dict:
    """One trend point from a single run's metrics.json."""
    m = metrics or {}
    sim = m.get("simulation", m)
    undefended, _ = count_undefended(m.get("significance_observe"))
    return {
        "date": date_str,
        "sharpe": sim.get("sharpe_ratio", sim.get("sharpe")),
        "accuracy_21d": m.get("accuracy_21d"),
        "avg_alpha_21d": m.get("avg_alpha_21d"),
        "n_undefended": undefended,
    }


def build_trend_rows(per_date: dict[str, dict]) -> list[dict]:
    """Trend points for a {date_str: metrics} map, oldest → newest."""
    return [trend_row(d, per_date[d]) for d in sorted(per_date)]


def render_trend(per_date: dict[str, dict], *, n_shown: int, n_total: int) -> None:
    """Streamlit cross-date trend section. `per_date` is the (already capped) map
    of {date: metrics}; n_shown/n_total drive the no-silent-truncation caption."""
    import streamlit as st

    st.subheader(f"Cross-Date Trend (last {n_shown} runs)")
    if n_total > n_shown:
        st.caption(f"Showing the most recent {n_shown} of {n_total} backtest runs.")
    rows = build_trend_rows(per_date)
    if not rows:
        st.info("No backtest runs available for a trend.")
        return

    import pandas as pd
    df = pd.DataFrame(rows).set_index("date")
    series = [
        ("sharpe", "Sharpe"),
        ("accuracy_21d", "Signal accuracy 21d"),
        ("avg_alpha_21d", "Avg alpha 21d"),
        ("n_undefended", "# undefended promotions"),
    ]
    cols = st.columns(2)
    for i, (col, label) in enumerate(series):
        with cols[i % 2]:
            st.markdown(f"**{label}**")
            sub = df[col].dropna()
            if sub.empty:
                st.caption("no data")
            else:
                st.line_chart(sub, height=180)
