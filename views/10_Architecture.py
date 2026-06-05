"""
Alpha Engine — Architecture (private console)

Bird's-eye walk through the system: thesis, the three orchestrated
pipelines, per-module roles, and S3 data contracts. Designed as the
single page an interviewer can watch over screenshare to understand
what this is in 10 minutes.

Merges Workstream 2.2 (architecture diagrams) and 2.4 (how-it-works
narrative) of the presentation revamp plan into one surface — fewer
clicks during a demo, single screenshare destination.

Lives on console.nousergon.ai (Cloudflare Access-gated). Selected
sections may promote to the public site after screenshare validation;
for now it stays private so it can speak in module-specific detail
without disclosure-boundary concerns.
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import streamlit.components.v1 as components

from loaders.s3_loader import predictor_horizon_days

# Predictor's training horizon — read from manifest so display strings
# track config without code edits when the horizon shifts. Fallback is
# the active production state post Track A cutover (2026-05-09).
_PRED_H = predictor_horizon_days()



st.divider()

# ---------------------------------------------------------------------------
# Mermaid render helper — load once, reuse across diagrams
# ---------------------------------------------------------------------------

_MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs"


def render_mermaid(diagram: str, height: int = 500) -> None:
    components.html(
        f"""
        <div class="mermaid" style="background:#000; color:#eee; padding:16px;">
{diagram}
        </div>
        <script type="module">
          import mermaid from "{_MERMAID_CDN}";
          mermaid.initialize({{
            startOnLoad: true,
            theme: "dark",
            themeVariables: {{
              background: "#000000",
              primaryColor: "#1a73e8",
              primaryTextColor: "#eee",
              lineColor: "#888",
              fontFamily: "system-ui, sans-serif",
            }},
            sequence: {{ useMaxWidth: true, wrap: true }},
            flowchart: {{ useMaxWidth: true, htmlLabels: true }},
          }});
        </script>
        """,
        height=height,
        scrolling=True,
    )


# ---------------------------------------------------------------------------
# Hero — thesis + phase trajectory
# ---------------------------------------------------------------------------

st.markdown("### Architecture")
st.markdown(
    """
    Nous Ergon is a multi-agent autonomous trading system that researches
    stocks, predicts short-horizon market-relative alpha, executes trades,
    and tunes its own parameters from realized outcomes. Equities trading
    is the substrate — chosen because decisions, metrics, and outcomes are
    unambiguous and continuously verifiable, which makes the agentic
    pattern observable.

    **The artifact on display is the agentic engineering pattern.** Six
    modules communicate exclusively through S3 contracts; three Step
    Function pipelines orchestrate the weekly research/training cycle,
    the daily trading loop, and the post-market reconciliation. Every
    signal, prediction, and fill is instrumented and traceable from
    research to P&L.
    """
)

st.markdown(
    """
    | Phase | Focus | Status |
    |---|---|---|
    | Phase 1 | Build the system end-to-end | ✅ Complete |
    | Phase 2 | Reliability + measurement buildout | 🟡 **Current** |
    | Phase 3 | Parameter tuning toward sustained alpha | ⏳ Next |
    | Phase 4 | Live capital | ⏳ Gated on Phase 3 sustained outperformance |
    """
)

st.divider()

# ---------------------------------------------------------------------------
# System block diagram
# ---------------------------------------------------------------------------

st.markdown("## System overview")
st.caption(
    "Six modules, three orchestrated pipelines, one shared S3 bucket. "
    "Modules never call each other directly — every inter-module "
    "communication flows through versioned S3 contracts."
)

render_mermaid(f"""
flowchart LR
    subgraph Weekly["Weekly Saturday SF"]
        DATA1[Data Phase 1<br/>prices · macro · RAG ingest]
        RES[Research<br/>LangGraph multi-agent]
        DATA2[Data Phase 2<br/>alternative data]
        PRED[Predictor Training<br/>3 L1 GBMs + Ridge L2]
        BT[Backtester<br/>signal quality + param sweep]
    end

    subgraph Daily["Weekday SF"]
        ENRICH[Morning Enrich<br/>OHLCV + intraday]
        INF[Predictor Inference<br/>{_PRED_H}d alpha + veto]
        PLAN[Morning Planner<br/>order book]
        DAEMON[Intraday Daemon<br/>sole order executor]
    end

    subgraph EOD["EOD SF"]
        REC[EOD Reconcile<br/>NAV + alpha vs SPY]
    end

    S3[(S3 contracts<br/>signals · predictions ·<br/>weights · trades · configs)]

    DATA1 --> S3
    RES --> S3
    DATA2 --> S3
    PRED --> S3
    BT --> S3
    S3 --> RES
    S3 --> PRED
    S3 --> INF
    S3 --> PLAN
    S3 --> DAEMON
    S3 --> REC
    INF --> S3
    DAEMON --> S3
    REC --> S3
    BT -.config auto-apply.-> S3

    style S3 fill:#1a73e8,stroke:#1a73e8,color:#fff
    style RES fill:#222,stroke:#1a73e8,color:#eee
    style PRED fill:#222,stroke:#1a73e8,color:#eee
    style DAEMON fill:#222,stroke:#1a73e8,color:#eee
    style BT fill:#222,stroke:#1a73e8,color:#eee
""", height=560)

st.caption(
    "The dashed *config auto-apply* edge from Backtester → S3 is the "
    "system's learning loop — weekly param sweeps write optimized "
    "configs that downstream modules pick up on next cold-start."
)

st.divider()

# ---------------------------------------------------------------------------
# Pipeline sequences
# ---------------------------------------------------------------------------

st.markdown("## Three orchestrated pipelines")
st.caption(
    "Each pipeline is an AWS Step Function with sequential state "
    "execution + timeout guards. EventBridge fires the schedule; SNS "
    "alerts on any state failure."
)

st.markdown("### Saturday SF — weekly research + training")
st.caption("Fires Sat 09:00 UTC (Sat 02:00 PT — chosen so polygon T+1 daily aggregate has settled).")
render_mermaid("""
sequenceDiagram
    participant EB as EventBridge
    participant DP1 as Data Phase 1
    participant RES as Research
    participant DP2 as Data Phase 2
    participant PT as Predictor Training
    participant BT as Backtester
    participant EJ as Eval Judge
    participant DD as Drift Detection
    participant SNS as Notify Complete

    EB->>DP1: cron(0 9 ? * SAT *)
    DP1->>DP1: prices · macro · constituents · RAG ingest
    DP1->>RES: complete
    RES->>RES: 6 sector teams + macro + CIO entrant gate
    RES->>DP2: signals.json
    DP2->>PT: alternative data ready
    PT->>PT: 3 L1 GBMs + Ridge L2 walk-forward
    PT->>BT: weights/meta promoted
    BT->>BT: backtest + param sweep + evaluator (consolidated)
    BT->>EJ: graders ready
    EJ->>DD: rolling-mean metrics
    DD->>SNS: SF + CFN drift scan complete
    SNS-->>EB: success email
""", height=540)

st.markdown("### Weekday SF — daily trading loop")
st.caption("Fires Mon-Fri 12:45 UTC (5:45 AM PT). Halt on NYSE holidays.")
render_mermaid(f"""
sequenceDiagram
    participant EB as EventBridge
    participant DDC as Deploy Drift Check
    participant EC2 as Start Executor EC2
    participant CTD as Check Trading Day
    participant ME as Morning Enrich
    participant INF as Predictor Inference
    participant MP as Morning Planner
    participant D as Daemon

    EB->>DDC: cron(45 12 ? * MON-FRI *)
    DDC->>DDC: block on weights/SF stamp drift
    DDC->>EC2: start trading instance
    EC2->>CTD: instance ready
    CTD->>ME: NYSE trading day
    ME->>ME: OHLCV + intraday → daily_closes/{{date}}.parquet
    ME->>INF: data ready
    INF->>INF: {_PRED_H}d alpha + veto signals + email
    INF->>MP: predictions/{{date}}.json
    MP->>MP: risk guard + position sizing
    MP->>D: order book written
    D->>D: intraday triggers + sole executor
    Note over D: ~1:05 PT shutdown<br/>triggers EOD SF
""", height=520)

st.markdown("### EOD SF — post-market reconcile")
st.caption("Triggered by daemon shutdown after market close (~1:05 PM PT).")
render_mermaid("""
sequenceDiagram
    participant D as Daemon Shutdown
    participant PMD as Post-Market Data
    participant REC as EOD Reconcile
    participant STOP as Stop Trading Instance

    D->>PMD: shutdown signal
    PMD->>PMD: settle EOD prices
    PMD->>REC: data ready
    REC->>REC: NAV + return vs SPY + alpha + email
    REC->>STOP: complete
    STOP-->>STOP: instance stopped
""", height=380)

st.divider()

# ---------------------------------------------------------------------------
# Per-module narrative cards
# ---------------------------------------------------------------------------

st.markdown("## Modules and alpha contribution")
st.caption("How each module produces or filters signal that contributes to long-term alpha vs SPY.")


def _module_card(emoji: str, name: str, repo_path: str, role: str, contribution: str) -> None:
    repo_url = f"https://github.com/cipher813/{repo_path}"
    st.markdown(
        f"""
        <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
                    border-radius: 8px; padding: 18px 20px; margin: 14px 0;">
            <div style="display:flex; align-items:baseline; gap:12px; margin-bottom:8px;">
                <span style="font-size:22px;">{emoji}</span>
                <strong style="font-size:16px;">{name}</strong>
                <a href="{repo_url}" target="_blank" style="font-size:12px; margin-left:auto; color:#1a73e8;">
                    {repo_path} ↗
                </a>
            </div>
            <div style="color:#bbb; font-size:14px; margin-bottom:8px;"><em>{role}</em></div>
            <div style="color:#ddd; font-size:14px;">{contribution}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


_module_card(
    "🔬", "Research", "alpha-engine-research",
    "Autonomous multi-agent investment research pipeline.",
    "Generates the primary signal universe — composite attractiveness scores combining quant + qual sub-scores with per-sector macro multipliers. LangGraph fan-out across 6 sector teams (Tech / Healthcare / Financials / Industrials / Consumer / Defensives) running Quant Analyst → Qual Analyst → Peer Review in parallel; CIO batch evaluation gates entrants. Scoring weights are auto-tuned by the backtester, closing the learning loop."
)
_module_card(
    "📊", "Predictor", "alpha-engine-predictor",
    f"Stacked meta-ensemble for {_PRED_H}d log-domain canonical alpha.",
    "Three Layer-1 specialized models (LightGBM volatility, deterministic momentum baseline, research-score GBM) feed a Layer-2 Ridge meta-learner trained on log-domain risk-matched canonical alpha labels at the configured horizon. Cross-sectional rank normalization on L1 inputs; walk-forward validation. Adds a quantitative ML overlay on top of research signals; high-confidence DOWN predictions trigger a veto gate that overrides BUY signals to avoid declining positions."
)
_module_card(
    "💱", "Executor", "alpha-engine",
    "Risk-gated order book + intraday execution.",
    "Translates signals + predictions into portfolio positions while enforcing risk constraints. Morning planner applies risk guard + position sizing; intraday daemon is the sole order executor — uses technical triggers (pullback / VWAP / support / time expiry) to time entries and executes exits at market open. Risk guard prevents over-concentration and shuts down new entries during drawdowns, preserving capital for recovery."
)
_module_card(
    "🔁", "Backtester", "alpha-engine-backtester",
    "Signal quality analysis + autonomous parameter optimization.",
    "Closes the feedback loop. Validates that signals correlate with outperformance, identifies which sub-scores are most predictive, and auto-applies optimized configs back to S3 — Research scoring weights, Executor risk params, Predictor veto threshold, Research signal-boost params. Fully autonomous, no manual intervention. The 10-year synthetic-signal backtest using the predictor's L1 momentum GBM is the primary param-sweep substrate."
)
_module_card(
    "📥", "Data Platform", "alpha-engine-data",
    "Centralized weekly data collection + ArcticDB universe library.",
    "Refreshes the price cache, macro features, constituents, and the RAG corpus that backs the research agents. ArcticDB on S3 is the canonical feature store; legacy parquet remains as fallback. Producer of the Saturday SF Data Phase 1 + 2 states and the weekday SF Morning Enrich state."
)
_module_card(
    "📈", "Dashboard", "alpha-engine-dashboard",
    "Public site (nousergon.ai) + private console (this surface).",
    "Public surface presents the Phase-2 reliability narrative + honest paper-trading performance. Private console (gated by Cloudflare Access) is the operator surface — Portfolio / Signals & Research / Predictor / Eval Quality / Metrics validation pages, plus this Architecture deep-dive. Read-only — no trading decisions made here."
)

st.divider()

# ---------------------------------------------------------------------------
# S3 data contracts
# ---------------------------------------------------------------------------

st.markdown("## S3 data contracts")
st.caption(
    "Modules communicate exclusively through S3. Path/schema changes "
    "follow the contract-safety rules in `~/Development/CLAUDE.md`: "
    "dual-write old + new for ≥1 week on path changes; only ADD fields "
    "(never rename/remove); coordinate breaking changes consumer-first."
)

st.markdown("""
| Path | Producer | Consumer(s) | Cadence |
|---|---|---|---|
| `signals/{date}/signals.json` | Research | Predictor + Executor | Weekly (Sat) |
| `predictor/predictions/{date}.json` | Predictor inference | Executor | Daily (weekday) |
| `predictor/weights/meta/` | Predictor training | Predictor inference | Weekly (Sat) |
| `predictor/price_cache_slim/*.parquet` | Data Platform | Predictor inference | Weekly |
| `predictor/daily_closes/{date}.parquet` | Data Platform (Morning Enrich) | Predictor inference | Daily (weekday) |
| `trades/trades_full.csv` | Executor (trade logger) | Dashboard + Backtester | Continuous |
| `trades/eod_pnl.csv` | Executor (EOD reconcile) | Dashboard + Backtester | Daily |
| `backtest/{date}/grading.json` | Backtester evaluator | Dashboard | Weekly (Sat) |
| `config/scoring_weights.json` | Backtester optimizer | Research | Weekly (Sat) |
| `config/executor_params.json` | Backtester optimizer | Executor | Weekly (Sat) |
| `config/predictor_params.json` | Backtester optimizer | Predictor | Weekly (Sat) |
| `archive/universe/{TICKER}/` | Research | Research (memory) + Dashboard | Per signal |
| `rag/manifest/{date}.json` | Data Platform (RAG ingest) | Dashboard | Weekly (Sat) |
| `uptime/{date}.json` | Executor (uptime tracker) | Dashboard | Daily |
""")

st.divider()

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.markdown(
    """
    Deeper material: each module's [public README](https://github.com/cipher813)
    plus the [system overview](https://github.com/cipher813/alpha-engine-docs#readme)
    in alpha-engine-docs. Per-module trade-offs, failure modes, and decision logs
    live in the private interview kit.
    """
)

