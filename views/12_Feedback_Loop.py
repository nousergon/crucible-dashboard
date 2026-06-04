"""
Alpha Engine — Feedback Loop (private console)

Shows that the system tunes itself. Every Saturday the backtester runs
parameter sweeps over the executor strategy + scoring weights + research
agent flags, evaluates each combination on Sharpe/alpha, and writes
the winners back to S3. Downstream modules pick up the new configs on
next cold-start. No human in the loop.

Per plan §3.3: this is the strongest single demonstration that alpha
generation is plausibly next, not aspirational — the *machinery* for
Phase 3 alpha tuning is already in place; what's missing is the
reliability buildout (Phase 2) for the inputs to be trustworthy
enough to tune on.

Sources from existing system outputs (Decision 11):
  • config/executor_params_history/{date}.json — the optimizer's
    chosen parameters + Sharpe / improvement% / n_combos rationale
  • config/research_params.json — CIO mode flag + reason
  • config/scoring_weights_history/{date}.json — Research scoring
    weight history (currently empty; will populate as the optimizer
    flips weights)

Lives on console.nousergon.ai (Cloudflare Access-gated). Showing
specific param values is fine here — the disclosure boundary
(`~/Development/CLAUDE.md`) gates *public* surfaces, not the gated
operator console.
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from loaders.s3_loader import (
    load_executor_params_history,
    load_research_params,
    load_scoring_weights,
    load_scoring_weights_history,
)



st.divider()

# ---------------------------------------------------------------------------
# Page intro + writeback flow diagram
# ---------------------------------------------------------------------------

st.markdown("### Feedback Loop")
st.markdown(
    """
    Every Saturday the backtester runs parameter sweeps over the
    executor strategy, the research scoring weights, and the research
    agent flags. It evaluates each combination on Sharpe and alpha,
    promotes winners that beat the prior baseline by a guarded margin,
    and writes the winners back to S3. Downstream modules pick up the
    new configs on next cold-start. **No human in the loop.**

    What you see below is the writeback log — every dated change the
    optimizer has made since the loop started running.
    """
)

components.html(
    """
    <div class="mermaid" style="background:#000; color:#eee; padding:12px;">
flowchart LR
    BT[Backtester<br/>Saturday SF] -->|sweep + grade| OPT[Optimizers]
    OPT -->|writeback| CFG[(config/*.json<br/>+ history/)]
    CFG -->|cold-start read| RES[Research]
    CFG -->|cold-start read| EXE[Executor]
    CFG -->|cold-start read| PRED[Predictor]
    RES --> S3[(S3 outputs)]
    EXE --> S3
    PRED --> S3
    S3 -->|next week| BT

    style CFG fill:#1a73e8,stroke:#1a73e8,color:#fff
    style BT fill:#222,stroke:#1a73e8,color:#eee
    style OPT fill:#222,stroke:#1a73e8,color:#eee
    </div>
    <script type="module">
      import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";
      mermaid.initialize({
        startOnLoad: true, theme: "dark",
        themeVariables: { background: "#000000", primaryColor: "#1a73e8",
                          primaryTextColor: "#eee", lineColor: "#888",
                          fontFamily: "system-ui, sans-serif" },
        flowchart: { useMaxWidth: true, htmlLabels: true },
      });
    </script>
    """,
    height=320,
    scrolling=False,
)

st.divider()

# ---------------------------------------------------------------------------
# Channel 1 — Executor parameter history
# ---------------------------------------------------------------------------

st.markdown("## 1. Executor parameters")
st.caption(
    "Source: `s3://alpha-engine-research/config/executor_params_history/"
    "{date}.json` (producer: `alpha-engine-backtester/optimizer/"
    "executor_optimizer.py`). Auto-scaled random search over the 6 core "
    "risk params; ranked by Sharpe; promoted on holdout validation."
)

exec_history = load_executor_params_history()

if not exec_history:
    st.info("No executor param history yet — the optimizer hasn't run a successful promotion.")
else:
    df = pd.DataFrame(exec_history).sort_values("updated_at").reset_index(drop=True)
    st.markdown(f"**{len(df)} dated promotions** since the optimizer started writing back.")

    # Display table
    display_cols = [c for c in [
        "updated_at", "min_score", "max_position_pct", "atr_multiplier",
        "time_decay_reduce_days", "time_decay_exit_days",
        "best_sharpe", "improvement_pct", "n_combos_tested",
    ] if c in df.columns]

    fmt_df = df[display_cols].copy()
    if "max_position_pct" in fmt_df.columns:
        fmt_df["max_position_pct"] = fmt_df["max_position_pct"].apply(
            lambda v: f"{float(v) * 100:.1f}%" if pd.notna(v) else "—"
        )
    if "best_sharpe" in fmt_df.columns:
        fmt_df["best_sharpe"] = fmt_df["best_sharpe"].apply(
            lambda v: f"{float(v):.3f}" if pd.notna(v) else "—"
        )
    if "improvement_pct" in fmt_df.columns:
        fmt_df["improvement_pct"] = fmt_df["improvement_pct"].apply(
            lambda v: f"{float(v) * 100:+.2f}%" if pd.notna(v) else "—"
        )
    fmt_df.columns = [
        "Date" if c == "updated_at" else c.replace("_", " ").title()
        for c in fmt_df.columns
    ]
    st.dataframe(fmt_df, use_container_width=True, hide_index=True)

    # Sparkline charts per numeric param
    st.markdown("**Parameter values over time**")
    numeric_params = [c for c in [
        "min_score", "max_position_pct", "atr_multiplier",
        "time_decay_reduce_days", "time_decay_exit_days",
    ] if c in df.columns]

    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")

    n = len(numeric_params)
    if n > 0:
        cols = st.columns(min(n, 3))
        for i, param in enumerate(numeric_params):
            col = cols[i % 3]
            with col:
                fig = go.Figure(go.Scatter(
                    x=df["updated_at"],
                    y=pd.to_numeric(df[param], errors="coerce"),
                    mode="lines+markers",
                    line=dict(color="#1a73e8", width=2),
                    marker=dict(size=8),
                ))
                fig.update_layout(
                    title=dict(text=param.replace("_", " "), font=dict(size=13)),
                    height=180,
                    margin=dict(l=10, r=10, t=30, b=10),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
                    yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
                )
                st.plotly_chart(fig, use_container_width=True, key=f"sparkline_{param}")

    # Sharpe trajectory — the rationale
    if "best_sharpe" in df.columns and df["best_sharpe"].notna().any():
        sharpe_fig = go.Figure(go.Scatter(
            x=df["updated_at"],
            y=pd.to_numeric(df["best_sharpe"], errors="coerce"),
            mode="lines+markers",
            line=dict(color="#7fd17f", width=2),
            marker=dict(size=10),
        ))
        sharpe_fig.add_hline(y=0, line_dash="dot", line_color="#888")
        sharpe_fig.update_layout(
            title="Backtester best Sharpe at each promotion",
            height=260,
            margin=dict(l=10, r=10, t=40, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
            yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        )
        st.plotly_chart(sharpe_fig, use_container_width=True, key="sharpe_trajectory")
        st.caption(
            "Each marker is a Saturday promotion. The trajectory tells "
            "the Phase-2 story: early sweeps optimize against noisy "
            "data; later sweeps stabilize as the measurement substrate "
            "matures."
        )

st.divider()

# ---------------------------------------------------------------------------
# Channel 2 — Research agent params (CIO mode flag)
# ---------------------------------------------------------------------------

st.markdown("## 2. Research agent params")
st.caption(
    "Source: `s3://alpha-engine-research/config/research_params.json` "
    "(producer: `alpha-engine-backtester/optimizer/weight_optimizer.py`). "
    "When CIO ranking lift drops below baseline, the optimizer flips "
    "`cio_mode` to `deterministic` (sort by composite + sector caps); "
    "when it recovers, flips back to `rubric` (full Sonnet-judged "
    "advancement). Captures a channel where the *agent's behavior* is "
    "tuned by data, not just numeric parameters."
)

research_params = load_research_params() or {}
if research_params:
    rcol1, rcol2 = st.columns([1, 2])
    rcol1.metric(
        "CIO mode",
        str(research_params.get("cio_mode", "—")),
        delta=str(research_params.get("cio_mode_updated_at", "")) if research_params.get("cio_mode_updated_at") else None,
        delta_color="off",
    )
    with rcol2:
        st.markdown("**Rationale**")
        st.markdown(
            f'<div style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); '
            f'border-radius: 6px; padding: 12px; font-size: 14px; color: #ddd;">'
            f'{research_params.get("cio_mode_reason", "—")}'
            f'</div>',
            unsafe_allow_html=True,
        )
else:
    st.info("`research_params.json` not yet present in S3.")

st.divider()

# ---------------------------------------------------------------------------
# Channel 3 — Scoring weights
# ---------------------------------------------------------------------------

st.markdown("## 3. Research scoring weights")
st.caption(
    "Source: `s3://alpha-engine-research/config/scoring_weights.json` + "
    "`scoring_weights_history/{date}.json` (producer: "
    "`alpha-engine-backtester/optimizer/weight_optimizer.py`). The quant "
    "vs qual sub-score balance is auto-tuned weekly based on which "
    "sub-score correlates most with realized 10d/30d outperformance."
)

current_weights = load_scoring_weights() or {}
weights_history = load_scoring_weights_history() or []

wcol_a, wcol_b = st.columns(2)
with wcol_a:
    st.markdown("**Current**")
    if current_weights:
        clean_weights = {k: v for k, v in current_weights.items() if isinstance(v, (int, float))}
        if clean_weights:
            st.dataframe(
                pd.DataFrame(
                    [{"Component": k, "Weight": f"{float(v):.3f}"} for k, v in clean_weights.items()]
                ),
                use_container_width=True, hide_index=True,
            )
        else:
            st.json(current_weights)
    else:
        st.caption("`scoring_weights.json` not yet present in S3.")

with wcol_b:
    st.markdown("**History**")
    if weights_history:
        st.dataframe(
            pd.DataFrame(weights_history),
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption(
            "No historical promotions yet — the weight optimizer hasn't "
            "had a successful promotion that beat its baseline."
        )

st.divider()

# ---------------------------------------------------------------------------
# Promotion gate context
# ---------------------------------------------------------------------------

st.markdown("## Why every weekly run isn't a promotion")
st.markdown(
    """
    The optimizers don't blind-write whatever scored highest — each has a
    promotion gate. **Executor optimizer** validates the winner on a held-out
    split before writing back; **weight optimizer** requires improvement vs
    the prior baseline by a configurable margin before flipping. When the
    gate fails, the prior config stays in place and the run logs a
    "no-promotion" entry. That's what the gaps in the history above
    represent — Saturdays where the optimizer ran but didn't promote.

    The gate is the discipline that makes the loop trustworthy. Without
    it, every Saturday would shift the system around in response to noise.
    """
)

