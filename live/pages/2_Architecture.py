"""
Nous Ergon — Architecture (public)

Visual system walkthrough for the public site. Pre-call read for an
interviewer with 5–10 minutes who wants to understand what the system
is and how the pieces fit together.

Differentiation from the GitHub system README + the private console
Architecture page (pages/10_Architecture.py):
- README: static GitHub-rendered mermaid.
- This page: design-token-styled mermaid + S3 data contracts
  visualization + autonomous feedback loop visualization (the last
  two not on README).
- Private console: deeper detail, internal-only commentary, designed
  for live screenshare during the demo.

Stays narrow — visual deep-dive, not module narrative. Per-module deep
dives live on per-repo GitHub READMEs.
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st
import streamlit.components.v1 as components

from components.header import render_footer, render_header
from components.styles import inject_base_css

st.set_page_config(
    page_title="Architecture — Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_base_css()
render_header(current_page="Architecture")

st.divider()

# ---------------------------------------------------------------------------
# Mermaid render helper — load once per render, design-token-styled.
# ---------------------------------------------------------------------------

_MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs"


def render_mermaid(diagram: str, height: int = 460) -> None:
    """Render a mermaid diagram in a dark-themed iframe.

    Click directives inside the diagram (e.g. ``click ID href "URL"``)
    work — the script enables htmlLabels on flowchart so the link
    decorations render as clickable anchors.
    """
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
# Section 1 — Page intro
# ---------------------------------------------------------------------------

st.markdown(
    """
    The system flow at a glance — six modules, three Step Functions,
    autonomous parameter feedback. For the same system map rendered
    statically, see the
    [GitHub README](https://github.com/cipher813/alpha-engine-docs).
    For per-module deep dives, see the per-repo READMEs.
    """
)

st.divider()

# ---------------------------------------------------------------------------
# Section 2 — System architecture
# ---------------------------------------------------------------------------

st.markdown("### System architecture")
st.caption(
    "Six modules communicating exclusively through S3 — research, "
    "predictor, executor, backtester, dashboard, and the data layer that "
    "feeds them."
)

render_mermaid("""
flowchart LR
    Data[Data<br/>prices · macro · features<br/>RAG corpus]
    Research[Research<br/>6 sector teams + CIO + macro<br/>incl. LLM-as-judge]
    Predictor[Predictor<br/>L1 momentum/vol GBMs + research calibrator<br/>+ L2 Ridge meta-learner]
    Executor[Executor<br/>risk-gated sizing + intraday daemon]
    Backtester[Backtester<br/>eval + parity + 4 config optimizers]
    Dashboard[Dashboard<br/>nousergon.ai + console.nousergon.ai]

    Data --> Research
    Data --> Predictor
    Research --> Predictor
    Research --> Executor
    Predictor --> Executor
    Executor --> Backtester

    Backtester -.config auto-apply.-> Research
    Backtester -.config auto-apply.-> Predictor
    Backtester -.config auto-apply.-> Executor

    Data -.read-only.-> Dashboard
    Research -.read-only.-> Dashboard
    Predictor -.read-only.-> Dashboard
    Executor -.read-only.-> Dashboard
    Backtester -.read-only.-> Dashboard
""", height=520)

st.divider()

# ---------------------------------------------------------------------------
# Section 3 — Step Function pipelines
# ---------------------------------------------------------------------------

st.markdown("### Step Function pipelines")
st.caption(
    "Three orchestrated pipelines run on a fixed cadence. EventBridge "
    "fires the weekly + weekday triggers; daemon shutdown after the "
    "trading day fires the EOD pipeline (single authoritative path)."
)

st.markdown("**Weekly — `alpha-engine-saturday-pipeline`**")
st.caption("EventBridge `cron(0 9 ? * SAT *)` — Sat 09:00 UTC (Sat 02:00 AM PT)")

render_mermaid("""
flowchart LR
    Trigger((Sat<br/>09:00 UTC)) --> P1
    P1[DataPhase1<br/>EC2 SSM<br/>30 min] --> RAG
    RAG[RAGIngestion<br/>EC2 SSM<br/>30 min] --> R
    R[Research<br/>Lambda · 15 min<br/><i>incl. LLM-as-judge</i>] --> P2
    P2[DataPhase2<br/>Lambda<br/>10 min] --> Train
    Train[PredictorTraining<br/>EC2 spot<br/>90 min] --> BT
    BT[Backtester<br/>EC2 spot · 120 min<br/><i>eval + parity + 4 optimizers</i>] --> Notify((SNS))
""", height=300)

st.markdown("**Weekday morning — `alpha-engine-weekday-pipeline`**")
st.caption("EventBridge `cron(5 13 ? * MON-FRI *)` — 6:05 AM PT")

render_mermaid("""
flowchart LR
    Trigger((6:05 AM PT)) --> Inf
    Inf[PredictorInference<br/>Lambda] --> Start
    Start[StartExecutorEC2] --> Boot
    Boot[Trading EC2 boots<br/>systemd] --> Plan
    Plan[Executor Planner<br/>~6:15 AM PT] --> Daemon((Executor Daemon<br/>~6:20 AM PT))
""", height=240)

st.markdown(
    "The daemon runs through the trading day, executing urgent exits at "
    "open and timing entries via intraday triggers (pullback, VWAP, "
    "support, time-expiry). Daemon shutdown after close (~1:15 PM PT) "
    "triggers the EOD pipeline."
)

st.markdown("**EOD — `alpha-engine-eod-pipeline`**")
st.caption("Triggered by daemon shutdown — single authoritative path, no redundant cron")

render_mermaid("""
flowchart LR
    Trigger((Daemon shutdown<br/>~1:15 PM PT)) --> Post
    Post[PostMarketData<br/>SSM on ae-trading<br/><i>EOD OHLCV → ArcticDB</i>] --> EOD
    EOD[EODReconcile<br/>NAV · α · positions<br/>trades.db + EOD email] --> Stop((StopTradingInstance))
""", height=240)

st.divider()

# ---------------------------------------------------------------------------
# Section 4 — S3 data contracts (NEW visualization)
# ---------------------------------------------------------------------------

st.markdown("### S3 data contracts")
st.caption(
    "The wires between modules — every output is a named S3 path; every "
    "consumer reads on cold-start. Schema changes are additive only; "
    "path changes dual-write for at least a week to preserve consumers."
)

render_mermaid("""
flowchart TB
    subgraph Producers
        D[Data]
        R[Research]
        P[Predictor]
        E[Executor]
        B[Backtester]
    end

    subgraph S3 ["s3://alpha-engine-research/"]
        Sig["signals/{date}/signals.json"]
        Pred["predictor/predictions/{date}.json"]
        Trades["trades/eod_pnl.csv<br/>trades/trades_full.csv"]
        Cfg["config/scoring_weights.json<br/>config/executor_params.json<br/>config/predictor_params.json<br/>config/research_params.json"]
        Arc["ArcticDB universe library<br/>predictor/price_cache_slim/<br/>predictor/daily_closes/"]
    end

    D --> Arc
    R --> Sig
    P --> Pred
    E --> Trades
    B --> Cfg

    Sig --> P
    Sig --> E
    Pred --> E
    Cfg --> R
    Cfg --> P
    Cfg --> E
    Arc --> P
    Arc --> B
    Arc --> E
""", height=560)

st.divider()

# ---------------------------------------------------------------------------
# Section 5 — Autonomous feedback loop (NEW visualization)
# ---------------------------------------------------------------------------

st.markdown("### Autonomous feedback loop")
st.caption(
    "What turns a static system into a learning one. The backtester "
    "evaluates the system's own outputs each week, runs parameter "
    "sweeps, validates on holdout, and writes four optimized configs "
    "back to S3 — Research / Predictor / Executor read on cold-start. "
    "The mechanism that makes Phase 3 alpha tuning a configuration "
    "flip rather than a code change."
)

render_mermaid("""
flowchart LR
    System[Live system outputs<br/>signals · predictions<br/>fills · P&L]
    Eval[Backtester evaluator<br/>weekly]
    Sweep[Parameter sweeps<br/>random search × Sharpe<br/>holdout validation]
    Cfg[4 optimized configs &rarr; S3<br/>scoring weights<br/>executor params<br/>predictor veto<br/>research params]
    Read[Research / Predictor / Executor<br/>read on cold-start]

    System --> Eval
    Eval --> Sweep
    Sweep -- holdout pass --> Cfg
    Cfg --> Read
    Read -.next week's behavior delta.-> System
""", height=320)

st.divider()

# ---------------------------------------------------------------------------
# Footer pointer
# ---------------------------------------------------------------------------

st.markdown(
    """
    Implementation lives in seven public repos indexed from
    [`alpha-engine-docs`](https://github.com/cipher813/alpha-engine-docs).
    Live system status: [Home](/). Curated production retros:
    [Retros](/Retros).
    """
)

render_footer()
