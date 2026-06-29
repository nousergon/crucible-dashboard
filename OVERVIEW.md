# alpha-engine-dashboard — Code Index

> Index of entry points, key files, and data contracts. Companion to [README.md](README.md). System overview lives in [`alpha-engine-docs`](https://github.com/nousergon/nousergon-docs).

## Module purpose

Read-only Streamlit monitoring — portfolio, signals, predictor, execution, eval quality. Powers public `nousergon.ai` live console (transitional — moves to `live.nousergon.ai` once the Astro marketing site lands at apex) + private `console.nousergon.ai`.

## Entry points

| File | What it does |
|---|---|
| [`app.py`](app.py) | Private dashboard root — multipage Streamlit app |
| [`live/app.py`](live/app.py) | Live-console root — powers nousergon.ai today, moving to `live.nousergon.ai` once Astro lands at apex |
| [`health_checker.py`](health_checker.py) | Cron'd health checker that surfaces module freshness |

## Where things live

| Concept | File |
|---|---|
| S3 read helpers (cached) | [`loaders/s3_loader.py`](loaders/s3_loader.py) |
| SQLite (research.db, trades.db) loader | [`loaders/db_loader.py`](loaders/db_loader.py) |
| Signal loader (signals.json + score_performance) | [`loaders/signal_loader.py`](loaders/signal_loader.py) |
| LLM-as-judge eval artifact loader | [`loaders/eval_loader.py`](loaders/eval_loader.py) |
| Loader utilities | [`loaders/utils.py`](loaders/utils.py) |
| NAV chart (Portfolio vs SPY) | [`charts/nav_chart.py`](charts/nav_chart.py) |
| Daily / cumulative alpha chart | [`charts/alpha_chart.py`](charts/alpha_chart.py) |
| Portfolio composition + drawdown | [`charts/portfolio_chart.py`](charts/portfolio_chart.py) |
| Signal accuracy chart | [`charts/accuracy_chart.py`](charts/accuracy_chart.py) |
| Predictor IC + per-L1 component IC | [`charts/predictor_chart.py`](charts/predictor_chart.py) |
| Sub-score attribution chart | [`charts/attribution_chart.py`](charts/attribution_chart.py) |
| Console pages (full set, grouped into 8 `st.navigation` sections) | [`views/`](views/) — wired in [`app.py`](app.py) |
| Portfolio page (NAV, alpha, drawdown, positions) | [`views/1_Portfolio.py`](views/1_Portfolio.py) |
| Signals + research thesis timeline | [`views/2_Signals_and_Research.py`](views/2_Signals_and_Research.py) |
| Signal-quality / regime / score-bucket analysis | [`views/3_Analysis.py`](views/3_Analysis.py) |
| System health (pipeline status, deploys) | [`views/4_System_Health.py`](views/4_System_Health.py) |
| Execution quality (fills, triggers, slippage) | [`views/6_Execution.py`](views/6_Execution.py) |
| Predictor predictions + IC trend | [`views/7_Predictor.py`](views/7_Predictor.py) |
| LLM-as-judge eval quality | [`views/8_Eval_Quality.py`](views/8_Eval_Quality.py) |
| Shared process-archive renderer (research/predictor/backtester briefing archives) | [`components/process_archive.py`](components/process_archive.py) |
| Number formatting helpers | [`shared/formatters.py`](shared/formatters.py) |
| Per-position P&L computation | [`shared/position_pnl.py`](shared/position_pnl.py) |
| Accuracy metric helpers | [`shared/accuracy_metrics.py`](shared/accuracy_metrics.py) |
| Shared normalization helpers | [`shared/normalizers.py`](shared/normalizers.py) |
| Shared constants | [`shared/constants.py`](shared/constants.py) |
| Live-console Streamlit theme + CSS | [`live/.streamlit/config.toml`](live/.streamlit/config.toml), [`live/components/styles.py`](live/components/styles.py) |
| Live-console docs (rendered on /Docs page) | [`live/docs/`](live/docs/) |
| Trading-calendar helpers | [`trading_calendar.py`](trading_calendar.py) |
| SSM secret loader | [`ssm_secrets.py`](ssm_secrets.py) |

## Inputs / outputs

### Reads (only — no writes)
| Source | Path |
|---|---|
| Research signals | `s3://alpha-engine-research/signals/{date}/signals.json` |
| Research thesis history + IC audit | `s3://alpha-engine-research/research.db` |
| Predictor predictions | `s3://alpha-engine-research/predictor/predictions/{date}.json` + `latest.json` |
| Predictor metrics (L2 + per-L1 IC) | `s3://alpha-engine-research/predictor/metrics/latest.json` |
| Trade audit log | `s3://alpha-engine-research/trades/trades_full.csv` |
| Daily NAV / α / positions | `s3://alpha-engine-research/trades/eod_pnl.csv` |
| Backtest reports + grades | `s3://alpha-engine-research/backtest/{date}/`, `backtest/grade_history.json` |
| LLM-as-judge eval artifacts | `s3://alpha-engine-research/eval_artifacts/{date}/` |
| Module health status markers | `s3://alpha-engine-research/health/*.json` |
| Aggregated cost parquet | `s3://alpha-engine-research/decision_artifacts/_cost/{date}/cost.parquet` |

## Run modes

| Mode | Where | Command |
|---|---|---|
| Live console (nousergon.ai today, `live.nousergon.ai` post-Astro) | EC2 `ae-dashboard` (always-on, port 8502) | `nous-ergon-live.service` (systemd) runs `live/app.py` |
| Private dashboard (`console.nousergon.ai`) | Same EC2, separate Streamlit instance (port 8501) | `dashboard.service` runs `app.py`; Cloudflare Access in front |
| Local dev | venv | `streamlit run app.py` (or `streamlit run live/app.py`) |
| Health check | EC2 cron | `python health_checker.py` (6-hourly with SNS on stale data) |

Deploy: `git push origin main && ae-dashboard "cd ~/alpha-engine-dashboard && git pull && sudo systemctl restart streamlit-*"`. TTL caching: 15 min for signals + trades, 1 hr for research + backtest.

## Tests

`pytest tests/` covers loader shape (S3 + SQLite roundtrips), accuracy-metric math, position-P&L computation, formatter edge cases, and chart-builder smoke tests. ~218 tests, ~81% coverage.
