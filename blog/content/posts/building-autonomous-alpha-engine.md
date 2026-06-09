---
title: "Nous Ergon: Building an Autonomous Alpha Engine with AI"
date: 2026-03-15
description: "A fully autonomous trading system combining LLM-driven research, quantitative ML prediction, and deterministic execution to generate alpha over the S&P 500."
tags: ["ai-engineering", "llm", "trading", "system-design", "machine-learning"]
canonical_url: "https://nousergon.ai/blog/posts/building-autonomous-alpha-engine/"
ShowToc: true
TocOpen: true
---

## The Thesis

Can AI generate sustained market alpha — not through a single model making predictions, but through a system of specialized components, each contributing what it does best?

That's the question behind **Nous Ergon: Alpha Engine** (νοῦς ἔργον — "intelligence at work"), a fully autonomous trading system I've been building that combines AI-driven research, quantitative prediction, and rule-based execution. Quantitative finance — using mathematical models and statistical analysis to make investment decisions — has traditionally been the domain of institutional hedge funds with massive engineering teams. Large language models and modern machine learning tooling are changing that equation.

The system's north star is **alpha** — the difference between the portfolio's return and the S&P 500 (SPY):

```
Alpha = Portfolio Return − SPY Return
```

Positive alpha means you're doing something the market isn't already pricing in. Everything in Nous Ergon — every agent prompt, every feature, every risk rule — exists to find, validate, and capture that edge.

## Why Not Just Ask an LLM to Trade?

The naive approach is tempting: give a large language model (LLM) market data and ask it what to buy. But LLMs are probabilistic text generators. They excel at synthesis, judgment, and reasoning across unstructured information. They're terrible at precise numerical prediction, risk management, and consistent execution.

Nous Ergon splits the problem into three layers, each matched to the right tool:

| Layer | Tool | Why |
|-------|------|-----|
| **Research** | LLM agents (Claude) | Judgment over unstructured data — news, analyst reports, macro context |
| **Prediction** | Machine learning (ML) ensemble (starting with LightGBM) | Pattern recognition over structured numerical features |
| **Execution** | Deterministic rules | Hard risk constraints that never get creative |

LLMs reason about *why* a stock might move. ML models find *patterns* in how stocks actually move. And risk rules ensure the system survives long enough for the other two to matter.

A key design decision: LLM agents are used *only* in the Research module. This deliberate separation means the Predictor, Executor, and Backtester can run unlimited simulations, parameter sweeps, and backtests without making a single LLM API call. When you're iterating on model features or testing risk parameters, that cost decoupling matters.

## The Five Modules

Nous Ergon runs as five modules on AWS, connected through a shared S3 bucket. Each module has a single job, reads its inputs from S3, and writes its outputs back. There's no shared state beyond the bucket — no databases to coordinate, no APIs to call between services.

### 1. Research

Five LLM agents orchestrated by LangGraph maintain rolling investment theses on ~20 tracked stocks and scan ~900 S&P 500 and S&P 400 tickers weekly for the top buy candidates. A quantitative filter first reduces the ~900 universe to ~50 candidates using volume, price, and momentum screens — no LLM calls. From there, a ranking agent (Sonnet) compares all ~50 candidates in a single cross-stock evaluation and selects the top ~35. Then two per-ticker agents — news sentiment and analyst research — each run independently on every candidate and population stock (Haiku), producing the sub-scores that feed into the final composite. A macro agent (Sonnet) assesses the broader market environment and sector conditions. A consolidator agent (Sonnet) synthesizes all analyses into a research brief.

Research outputs a composite attractiveness score (0–100) per ticker, combining news sentiment (50%) and analyst research (50%), with per-sector macro adjustments. The resulting `signals.json` — written to Amazon S3 — is the system's primary input for everything downstream.

Research focuses entirely on fundamental attractiveness over a 6–12 month horizon. Technical analysis is deliberately excluded from the composite score. This is the first half of what I call *horizon separation* — Research answers "is this a good stock?", not "is now the right time to buy it?"

### 2. Predictor

The Predictor handles the second half of horizon separation: short-term technical timing. Research may identify a stock as fundamentally attractive over the next 6–12 months, but that doesn't mean today is the right day to enter. Each trading day, the Predictor evaluates the population's near-term momentum using engineered features across technical indicators, macro context, volume analysis, and cross-sectional measures. Its **veto gate** can override a BUY signal from Research when the model predicts DOWN with high confidence — preventing the system from entering a fundamentally sound position at a technically poor time.

The current implementation uses a LightGBM gradient-boosted machine (GBM) model, but the architecture is designed for an ensemble of ML and deep learning algorithms. The plan is to layer additional models — likely including a neural network — and combine their predictions through confidence-weighted voting. LightGBM is a strong starting point: it handles threshold interactions and missing data well, trains fast, and provides interpretable feature importance. As the system matures, adding models that capture different types of patterns (non-linear interactions, sequential dependencies) should improve prediction quality.

The model trains on sector-neutral labels — stock returns minus sector exchange-traded fund (ETF) returns — isolating stock-specific signal from sector momentum. Weekly retraining uses 10 years of price history. New model weights only promote to production if they pass an Information Coefficient (IC) gate. IC measures the rank correlation between predicted and actual returns — in financial ML, an IC of 0.03–0.05 is considered meaningful because even small persistent edges compound significantly when applied across many positions over time. The current validation gate requires IC > 0.03.

### 3. Executor

Once the Predictor clears a position for entry, the Executor takes over. It reads signals and predictions from S3, applies hard risk rules, sizes the position, and executes market orders on Interactive Brokers. From that point forward, the Executor owns the position — managing it through a set of deterministic rules until exit.

Risk management is graduated, not binary. A drawdown response system scales position sizing through tiers: full sizing in normal conditions, reduced sizing as drawdowns deepen, and a complete halt at -8%. Additional constraints cap individual positions (5% of net asset value (NAV), 2.5% in bear markets), sector concentration (25% NAV), and total equity exposure (90% NAV).

Exit management combines ATR-based trailing stops (volatility-adaptive) with time-decay rules that progressively tighten stops as positions age, forcing the system to either prove a thesis quickly or move on.

The Executor's rules are simple by design — but they aren't arbitrary. They're the output of the Backtester's systematic optimization (more on this below). The Executor doesn't reason or predict; it applies its parameters exactly as given, every time, with no emotional second-guessing. The intelligence lives in the process that *produces* those parameters, not in the component that executes them. This gives you consistent, repeatable execution while the learning happens offline where you can run thousands of simulations cheaply.

### 4. Backtester

The system's learning mechanism. Runs weekly to validate the entire pipeline end-to-end — not just "did we make money?" but "are our signals predictive, which components drive that predictiveness, and what execution parameters maximize risk-adjusted returns?"

The Backtester does this through several layers of analysis:

- **Signal quality**: are Research scores actually predictive? What percentage of BUY signals beat SPY at 10 and 30 days? Are higher scores more predictive than lower ones?
- **Attribution**: which sub-scores (news vs research) correlate with outperformance? This determines where the scoring formula's weight should shift.
- **Weight optimization**: adjusts the Research scoring weights based on attribution results — conservatively, with a 30/70 blend of data-driven recommendations against current weights and a 15% max change per weight.
- **Executor parameter optimization**: a parameter sweep across executor parameters — minimum entry score, position size limits, Average True Range (ATR) trailing stop multipliers, time-decay windows — replaying historical signals through the full executor simulation for each combination and ranking by Sharpe ratio. Random sampling (Bergstra & Bengio 2012) replaces exhaustive grid search: the number of trials auto-scales as a percentage of the total parameter space, with a statistical floor that guarantees a 95% probability of finding a top-5% combination regardless of grid size. The best-performing parameters get recommended for production.
- **Predictor threshold calibration**: sweeps the veto gate's confidence threshold across seven levels, measuring the trade-off between precision (correctly blocked losing trades) and missed alpha (incorrectly blocked winners).

Each optimization has guardrails — minimum sample sizes, minimum improvement thresholds, excluded parameters (the drawdown circuit breaker is never auto-tuned) — to prevent overfitting to noise.

The results flow back through S3: updated scoring weights that Research loads on its next run, optimized parameters that the Executor reads on cold-start, and calibrated thresholds that the Predictor uses for its veto gate. Without the Backtester, the system operates blind. This is the component that turns a static pipeline into an adaptive one.

### 5. Dashboard

A Streamlit application providing read-only visibility into the full system: portfolio performance vs SPY, signal quality trends, per-ticker research timelines, backtester results, and predictor metrics. The operational cockpit.

## How It All Connects

The modules run on AWS in two cadences — a daily trading loop and a weekly optimization cycle — with S3 as the sole communication bus:

**Daily Cadence (Mon–Fri)**

- **Predictor** (6:15 AM PT) — reads latest signals.json from S3
- **Executor** (6:30 AM PT) — reads predictions, trades on Interactive Brokers
- **EOD Reconcile** (1:05 PM PT) — captures NAV, computes daily return and alpha, sends email

**Weekly Cadence (Sunday/Monday)**

- **Research** — scans 900 tickers, rotates population, outputs signals.json
- **Predictor Training** — retrains on 10y history, promotes weights if IC > 0.03
- **Backtester** — signal quality analysis, weight optimization, parameter sweeps

**Always-On**

- **Dashboard** (Streamlit) — read-only monitoring of all modules via S3

Research runs weekly to refresh the tracked population and generate updated `signals.json`. During the daily trading loop, the Predictor reads the latest signals from S3 and the Executor reads the Predictor's output. Each module's output is the next module's input, and S3 acts as the contract between them.

S3 as the communication bus means any module can be replaced, rewritten, or tested independently. The Research module doesn't know or care that a LightGBM model reads its signals. The Executor doesn't know that five LLM agents generated the scores it's acting on. They agree on a JSON schema, and S3 handles the rest.

## The Feedback Loop

The most important architectural decision wasn't any individual module — it was connecting the Backtester's output back to the upstream components.

Every week, the Backtester measures whether the system's signals actually worked. It tracks the percentage of BUY signals that beat SPY over 10 and 30 days, runs attribution analysis to determine which scoring components are pulling their weight, and recommends adjustments.

These recommendations flow back through S3: updated scoring weights that Research loads on its next run, optimized parameters that the Executor reads on cold-start, and calibrated thresholds that the Predictor uses for its veto gate. The system observes its own performance and adapts — slowly, conservatively, with guardrails — but it adapts.

This is what separates Nous Ergon from a static trading bot. Most automated trading systems are write-once: you build a strategy, deploy it, and hope it keeps working. Nous Ergon is designed to be fully autonomous — no human in the trading loop, no manual approvals, no daily oversight required. It researches, predicts, trades, measures, and adjusts on its own.

## Where Things Stand

The infrastructure is built. All five modules are deployed on AWS, wired end-to-end, and running against live market data on Interactive Brokers paper trading. Research refreshes signals weekly. The Predictor and Executor run autonomously every trading day — Predictor scores the latest signals, Executor places trades, end-of-day (EOD) reconciliation measures performance.

Now comes the hard part: making it actually generate alpha.

Alpha capture runs as an experiment with a pre-committed bar: sustained outperformance against SPY in paper trading. The work it drives — refining signal quality, tuning the ML models, calibrating risk parameters, expanding the prediction ensemble, iterating on scoring weights — is the research program. When the experiment clears its bar, the plan is to transition to real capital in small amounts.

Building the infrastructure was the engineering challenge. Generating alpha is the research challenge. This is where it gets interesting.

## Areas for Further Development

Beyond refining the core system, there are several directions that could meaningfully expand Nous Ergon's capabilities:

**Prediction Ensemble.** The Predictor currently runs a single LightGBM model. The near-term goal is to build out an ensemble with additional ML and deep learning architectures. Options like Temporal Fusion Transformers (TFT) are compelling for their ability to model time-varying relationships, but may be cost-prohibitive at this stage — both in compute for training and in the engineering effort to deploy on Lambda. As the system generates alpha, there will be opportunities to invest in stronger deep neural network (DNN) architectures and higher-quality data APIs that aren't justifiable today.

**Retrieval-Augmented Generation (RAG).** Research agents currently see fresh data each run plus the last thesis snapshot, but have no persistent memory of historical patterns — past earnings surprises, sector rotation cycles, how a stock behaved during previous rate hike environments. A RAG layer could let agents retrieve relevant historical context during their analysis, producing more informed research.

**MCP Tool Use.** Currently, the pipeline pre-fetches data and passes it to agents for analysis. With Model Context Protocol (MCP), agents could query data sources on demand — pulling specific SEC filings, checking real-time options flow, or querying alternative data — as part of their reasoning process rather than being limited to a pre-determined data scope.

**Social Sentiment.** Financial Twitter/X surfaces market-moving information — earnings reactions, sector rotation narratives, retail sentiment — often faster than traditional news sources. Integrating social sentiment as an additional signal source for Research could expand the system's information surface area.

**Expanded Sources and Features.** Both Research and Predictor have room to grow their input data. Research could incorporate earnings call transcripts, insider trading filings, or institutional flow data. The Predictor's feature set could expand with alternative data sources — options market signals, credit spreads, or cross-asset correlations — that may carry predictive information the current 29 features don't capture.

Each of these represents both a system improvement and a meaningful engineering challenge. The modular architecture — S3 contracts between independent modules — means any of them can be pursued without disrupting the rest of the system.
