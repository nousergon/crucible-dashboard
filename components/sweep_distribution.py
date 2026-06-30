"""Sweep-score distribution panel (config#1444 item 3).

The executor optimizer is a RANDOM SEARCH — there is no iterative convergence
trajectory to trace. The institutionally-meaningful view is the *distribution*
of the trials' scores and where the selected (best) combo sits in that field:
a chosen combo far out in the right tail vs. one barely above the median tells
you how robust the optimizer's pick is. Built from the existing
``param_sweep.csv`` (no new persistence).
"""

from __future__ import annotations

from typing import Sequence


def _finite(scores: Sequence[float]) -> list[float]:
    out = []
    for s in scores:
        try:
            f = float(s)
        except (TypeError, ValueError):
            continue
        if f == f and f not in (float("inf"), float("-inf")):  # NaN/inf drop
            out.append(f)
    return out


def selected_percentile(scores: Sequence[float], selected: float | None) -> float | None:
    """Percentile rank of ``selected`` within ``scores`` (fraction ≤ selected, %)."""
    vals = _finite(scores)
    if not vals or selected is None:
        return None
    n_le = sum(1 for v in vals if v <= selected)
    return round(100.0 * n_le / len(vals), 1)


def sweep_summary(scores: Sequence[float], selected: float | None = None) -> dict:
    """Distribution summary of the sweep's trial scores."""
    vals = sorted(_finite(scores))
    n = len(vals)
    if n == 0:
        return {"n": 0}

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
        return vals[idx]

    best = selected if selected is not None else vals[-1]
    return {
        "n": n,
        "min": vals[0],
        "median": _pct(50),
        "p90": _pct(90),
        "max": vals[-1],
        "selected": best,
        "selected_percentile": selected_percentile(vals, best),
    }


def render(sweep_df, sharpe_col: str | None) -> None:
    """Streamlit histogram of trial scores + the selected combo's percentile."""
    import streamlit as st

    st.markdown("**Sweep-Score Distribution**")
    if sweep_df is None or getattr(sweep_df, "empty", True) or not sharpe_col or sharpe_col not in sweep_df.columns:
        st.caption("No sweep scores available for a distribution.")
        return

    import pandas as pd
    scores = pd.to_numeric(sweep_df[sharpe_col], errors="coerce").dropna().tolist()
    summ = sweep_summary(scores)
    if not summ.get("n"):
        st.caption("No finite sweep scores.")
        return

    pct = summ.get("selected_percentile")
    st.caption(
        f"{summ['n']} trials · selected Sharpe {summ['selected']:.2f} "
        f"(best of field; {pct:.0f}th percentile) · "
        f"median {summ['median']:.2f} · p90 {summ['p90']:.2f} · max {summ['max']:.2f}"
        if pct is not None else f"{summ['n']} trials"
    )
    try:
        import plotly.express as px
        fig = px.histogram(x=scores, nbins=min(40, max(10, summ["n"] // 3)),
                           labels={"x": sharpe_col})
        fig.add_vline(x=summ["selected"], line_color="green", line_dash="dash")
        fig.update_layout(height=240, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        st.bar_chart(pd.Series(scores).value_counts(bins=20).sort_index())
