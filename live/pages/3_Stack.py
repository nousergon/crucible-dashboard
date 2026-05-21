"""
Nous Ergon — Stack

Curated list of the non-default product choices in the system. The page
surfaces the picks a hiring manager or technical reader would actually
want to ask about; commodity primitives (S3, EventBridge, IAM, etc.) are
omitted by design.

Per the per-surface zones-of-responsibility matrix: this page owns the
flat product list with one-liner rationale. Architectural reasoning
(why ArcticDB beat parquet, why Step Functions beat Airflow) lives
deeper in the architecture decision log — this page links across to
that surface where it's worth the click.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

from components.header import render_footer, render_header
from components.styles import inject_base_css

st.set_page_config(
    page_title="Stack — Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_base_css()
render_header(current_page="Stack")

st.divider()

st.markdown("### Stack")

# ---------------------------------------------------------------------------
# Agentic layer
# ---------------------------------------------------------------------------

st.markdown("#### Agentic layer")

st.markdown(
    """
    - **LangGraph** — multi-agent orchestration with `Send()` fan-out
      semantics and custom dict-keyed reducers. Six sector teams +
      macro economist + CIO + LLM-as-judge run as graph nodes with
      typed state. Vanilla LangChain doesn't compose for parallel
      multi-team coordination with state merging.
    - **Anthropic Claude — tiered.** Haiku for per-ticker
      quant, qual, and peer-review (~12 calls per Saturday run);
      Sonnet only for synthesis (macro economist, CIO batch
      evaluation, nuanced judge cases). Sonnet is ~5× Haiku —
      reserving it for synthesis keeps per-run cost stable.
    - **LangSmith** — auto-tracing on every production LLM call. The
      trajectory validator polls LangSmith for graph-correctness
      invariants (required nodes present, sector_team_node appears
      exactly 6 times) post-`graph.invoke()`. Free tier covers a
      personal-scale workload.
    - **Pydantic** — typed agent outputs (`with_structured_output(...,
      include_raw=True)` with strict-mode parse-error contract across
      every LLM-output site).
    """
)

st.divider()

# ---------------------------------------------------------------------------
# Data + retrieval
# ---------------------------------------------------------------------------

st.markdown("#### Data & retrieval")

st.markdown(
    """
    - **ArcticDB** (over parquet on S3) — feature store for ~50
      features × ~900 tickers × 10y. Migration was driven by a real
      performance bottleneck: per-feature parquet scans on S3 were
      slow on the read side and OOMed c5.large training instances.
      ArcticDB's symbol-keyed range queries pull only the slice each
      consumer needs.
    - **Neon — serverless Postgres + pgvector.** RAG corpus for SEC
      filings, 8-Ks, earnings transcripts, and rolling investment
      theses. HNSW indexing for fast vector search; serverless tier
      means no idle compute cost when the weekly research run isn't
      firing.
    - **Voyage `voyage-3-lite`** — 512-dimensional embeddings tuned
      for financial-domain text. Cheaper than OpenAI's
      text-embedding-3-* tier with better fit on the SEC-filing
      corpus the qual agents retrieve over.
    - **Polygon.io** — primary market-data source for adjusted
      OHLCV and intraday data; yfinance retained as cross-validation
      only, not as a silent fallback (silent-fallback patterns have
      been a repeat offender for masking real failures and were
      retired across the data layer).
    - **FRED + FMP** — macro indicators and supplemental fundamentals.
    - **VectorBT** — historical portfolio simulation in the backtester.
    """
)

st.divider()

# ---------------------------------------------------------------------------
# ML tools
# ---------------------------------------------------------------------------

st.markdown("#### ML tools")

st.markdown(
    """
    - **Python 3.12 / 3.13** — primary language across all eight repos.
      Lambda runs 3.12; local development on 3.13.
    - **LightGBM** — Layer-1 gradient-boosted models in the predictor
      meta-ensemble (momentum + volatility).
    - **pandas + numpy** — feature engineering, signal scoring,
      metric computation.
    """
)

st.divider()

# ---------------------------------------------------------------------------
# Compute + orchestration
# ---------------------------------------------------------------------------

st.markdown("#### Compute & orchestration")

st.markdown(
    """
    - **AWS Step Functions** (over Airflow / Dagster / Prefect) —
      three pipelines (Saturday weekly research, weekday morning,
      EOD post-close) orchestrated as state machines. Native to the
      AWS account that already runs Lambda + EC2 + S3, so no new
      infrastructure to operate. Execution history is itself the
      audit trail.
    - **AWS Lambda** — stateless agent calls, predictor inference,
      LLM-as-judge, rationale clustering, replay-concordance,
      replay-counterfactual. Container-image deploys via ECR for
      the Python 3.12 research Lambda.
    - **AWS EC2** — `t3.small` for the executor daemon (stateful, holds
      the IB Gateway connection), `t3.micro` for the dashboard,
      `c5.large` spot for batch (predictor training + backtester).
      Right-sized per workload — when `predictor_data_prep` OOMed on
      c5.large, the response was a pandas refactor (~1.1 GB → ~91 MB
      resident), not a bump to xlarge.
    - **CloudFormation** — Infrastructure-as-Code for Step Functions,
      Lambda functions, IAM roles, EventBridge rules. Drift detector
      compares CloudFormation stamps against live AWS state weekly.
    """
)

st.divider()

# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------

st.markdown("#### Surfaces")

st.markdown(
    """
    - **Streamlit** — dashboard surface for both the public site (this
      one) and the private console (`console.nousergon.ai`). Pragmatic
      pick for a single-author project: writing a custom React
      dashboard would be weeks of work for the same read-only
      monitoring outcome.
    - **IBC (Interactive Brokers Gateway)** — paper-account broker
      connection on the executor host. Hard runtime check refuses
      to connect to non-paper accounts (account ID must start with
      "D") — defense against accidental live-account wiring.
    """
)

render_footer()
