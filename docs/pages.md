# Pages Reference

Per-page field and chart documentation.

---

## Overview (`app.py`)

Entry point. Designed for triage, not analysis — answer _"is everything working?"_ in 10 seconds. Detail pages handle deep dives.

### Pipeline Status

One compact row of module badges (🟢 ok / 🟡 degraded / 🔴 failed / ⚪ unknown) for research, predictor_training, predictor_inference, executor, eod_reconcile. Shows age since last success. Reads `health/{module}.json` from S3.

### Today's Activity

Five metric cards: Entries Approved, Entries Blocked, Exits / Covers, Vetoes, Trades Executed Today.

- Approved / blocked / exits from today's `order_book_summary.json`
- Vetoes counted from `predictions/{date}.json` using the current veto_confidence threshold from `config/predictor_params.json`
- Trades count from `trades_full.csv` filtered to today's date

### Key Metrics

Four KPI cards, no charts:

| Card | Source |
|------|--------|
| Portfolio NAV | `eod_pnl.portfolio_nav` (most recent row) |
| Daily Alpha vs SPY | `eod_pnl.daily_alpha_pct` (normalized to decimal) |
| Cumulative Alpha | `NAV[-1]/NAV[0] − SPY[-1]/SPY[0]` (falls back to `sum(daily_alpha_pct)`) |
| Model Hit Rate (30d) | `predictor/metrics/latest.json.hit_rate_30d_rolling` |

### Market Context

Regime, VIX, 10yr yield from `macro_snapshots` for today's date.

### Alerts

Only shown when non-empty:

- Failed modules (from health JSON)
- Modules with no health status
- Stale modules (last success > 48h)
- Current drawdown ≤ −5%
- Latest recent S3 error

---

## Page 1: Portfolio (`views/1_Portfolio.py`)

Answers: _how is the paper portfolio performing vs. SPY?_

### Charts

**NAV vs SPY** (`charts/nav_chart.py`)
- Cumulative return % from first eod_pnl row
- Green shading where portfolio > SPY; red where below
- Hover shows date, portfolio %, SPY %, alpha %

**Daily Alpha** (`charts/alpha_chart.py`)
- Bar chart: green bars for positive alpha days, red for negative
- Secondary axis: cumulative alpha line overlay

**Drawdown**
- Area chart of `(NAV - peak_NAV) / peak_NAV`
- Red fill; dashed horizontal line at circuit breaker threshold (`-8%` from config)

### Current Positions

Parsed from `eod_pnl.positions_snapshot` (JSON string). Joined with today's signals for current score.

| Column | Source |
|--------|--------|
| Ticker | positions_snapshot key |
| Shares | positions_snapshot |
| Market Value | positions_snapshot |
| % NAV | positions_snapshot |
| Score (latest) | today's signals.json |
| Return Since Entry | `(current price - entry_price) / entry_price` |

### Summary Stats

Computed from full eod_pnl history:

| Stat | Formula |
|------|---------|
| Total return | `(NAV_last / NAV_first) - 1` |
| Sharpe (annualized) | `mean(daily_return) / std(daily_return) * √252` (requires ≥30 rows) |
| Max drawdown | `min((NAV - peak_NAV) / peak_NAV)` |
| Best / worst day | Max/min of `daily_return_pct` |
| Days positive/negative | Count of positive/negative `daily_alpha_pct` |
| Avg daily alpha | `mean(daily_alpha_pct)` |

---

## Page 2: Signals & Research (`views/2_Signals_and_Research.py`)

Answers: _what are all the signals today and why, and what does the research say about a specific ticker?_

Merges the former Signals and Research pages. Signal table and sector ratings at the top; a ticker drilldown section below surfaces the former Research page content (full score history with sub-scores and signal markers, conviction history, performance outcomes, thesis timeline).

### Date Picker

Dropdown of all available `signals/{date}/` S3 prefixes, defaulting to most recent.

### Signal Table

Full universe from signals.json. Filterable by:
- Sector (multiselect)
- Signal type (multiselect)
- Minimum score (slider)

Stale signals shown with ⚠ badge. Predictor direction shown as UP ↑ / FLAT → / DOWN ↓ in a `Prediction` column (blank if no prediction available); `Confidence` column shown only when ≥ 0.65.

### Ticker Drilldown

Select a ticker below the signal table to surface:
- Thesis summary paragraph
- Sub-score horizontal bar chart (technical / news / research) — current snapshot
- Predictor probability bar: `p_up` (green) / `p_flat` (gray) / `p_down` (red) stacked horizontal; badge showing modifier applied or skipped with reason
- **Score history** (full): composite line (bold) + faint sub-score lines (technical/news/research) + signal markers (ENTER ▲ / EXIT ▼ / REDUCE ◆)
- **Conviction history** line chart
- **Performance outcomes** table from `score_performance` (score_date, composite_score, return_10d/30d vs SPY, beat_spy_10d/30d as ✅/❌/⏳)
- **Thesis timeline** — expandable list of `thesis_summary` entries from `investment_thesis`, newest first

### Sector Ratings

| Column | Source |
|--------|--------|
| Sector | signals.json sector_ratings keys |
| Rating | OW / MW / UW |
| Modifier | +/- modifier |
| Rationale | Snippet from signals.json |

Color: OW = green, UW = red, MW = neutral.

---

## Page 3: Analysis (`views/3_Analysis.py`)

Answers: _are signals predictive, how did the backtester do, and is the pipeline learning?_

Merges the former Signal Quality, Backtester, and Evaluation pages. A shared backtest date selector sits at the top; three tabs organize the content.

### Signal Accuracy tab

**Note:** Meaningful after ~Week 4 (~200 rows with `beat_spy_10d` populated). Shows a data loading banner until then.

Charts (`charts/accuracy_chart.py`):

- **Accuracy Trend** — rolling 4-week accuracy (10d and 30d), dashed 50% reference line, shaded band at 55%+
- **Accuracy by Score Bucket** — grouped bars for 60–70, 70–80, 80–90, 90+ (10d and 30d)
- **Accuracy by Regime** — grouped bars (bull/neutral/bear/caution) joining `score_performance` to `macro_snapshots`
- **Alpha by Market Regime** — from `eod_pnl.csv` joined to `macro_snapshots`
- **Alpha Distribution** — histogram of `return_10d - spy_10d_return` with mean/median lines (score ≥ 70 and all signals panels)

Predictor accuracy charts have moved to the Predictor page (Phase 6).

### Backtester tab

Shows the selected backtest run output.

- **Last Run Summary** — run date, strategy, data range, universe size, runtime, status from `metrics.json`
- **Portfolio Simulation Stats** — total return, Sharpe, max drawdown, win rate, avg alpha, num trades
- **Parameter Sweep — Sharpe Heatmap** — X: `min_score`, Y: `max_position_pct`, color: Sharpe. One inner tab per `drawdown_circuit_breaker` value. Top 5 combinations table per tab. Source: `param_sweep.csv`
- **Signal Quality Summary** — accuracy 10d/30d, avg alpha 10d/30d from `metrics.signal_quality`; detail table from `signal_quality.csv`
- **Sub-Score Attribution** — horizontal bar chart of each sub-score's correlation with beat_spy_10d/30d. Source: `attribution.json`
- **Scoring Weights** — current weight metric cards from `scoring_weights.json`, weight recommendations table (current vs suggested with direction), weight history chart from `config/scoring_weights_history/{date}.json`
- **Raw Report** — collapsible expander rendering `report.md`

### Pipeline Evaluation tab

Structured visualizations for Phase 2/3/4 backtester metrics. Parses sections from `report.md`.

**1. Pipeline Lift — Decision Boundary Analysis**
- Waterfall chart of lift at each stage (Scanner → Teams → CIO → Predictor → Executor → Full Pipeline)
- Raw lift report expander

**2. Component Diagnostics** — six sub-tabs:
- Entry Triggers — scorecard from `report.md`
- Exit Timing — exit timing analysis
- Veto Value — net veto value
- Alpha Distribution — magnitude and score calibration
- Shadow Book — risk guard shadow book entries
- Macro A/B — macro multiplier evaluation

**3. Self-Adjustment Mechanisms** — live state from S3 configs:
- **Executor adjustments**: disabled triggers, p_up sizing status (IC), sizing A/B results from report. Source: `config/executor_params.json`
- **Research adjustments**: scanner params, team slot allocation, CIO mode (llm/deterministic). Sources: `config/scanner_params.json`, `config/team_slots.json`, `config/research_params.json`
- Full Phase 4 Report expander

---

## Page 4: System Health (`views/4_System_Health.py`)

Answers: _is the plumbing working?_

Merges the former Data Inventory and Feature Store pages. Two tabs:

### Modules & Data tab

- **Module Health & Freshness** — freshness table for research, predictor_training, predictor_inference, executor, eod_reconcile. Status colored (ok/degraded/failed). Reads `health/{module}.json` from S3.
- **Data Volume Growth** — dataset record counts (research.db tables + S3 object counts), cumulative trading days and cumulative trade records line charts.
- **Feedback Loop Maturity** — optimizer progress table with status (Active/Collecting/Blocked/Deferred) and progress bars against each threshold.
- **Data Manifests** — expandable JSON view of latest manifest per module. Reads `data_manifest/{module}/*.json`.
- **Missing Data Alerts** — missing EOD trading days, failed/unknown modules, unresolved score_performance rows. Shows success banner when nominal.

### Feature Store tab

Pre-computed feature snapshots for GBM inference — freshness, coverage, and drift monitoring.

- **Freshness** — latest snapshot date, age, schema version/hash from `features/{date}/schema_version.json`
- **Coverage** — per-group table (Technical / Interaction / Macro / Alternative / Fundamental) with ticker count, feature count, last updated timestamp, null count, status
- **Feature Catalog** — per-group expander listing each feature with description, source, refresh cadence, mean, std, nulls. Descriptions from `features/registry.json`.
- **Feature Distributions** — summary stats table, histogram for selected feature, training baseline comparison with z-score drift flag
- **Drift Detection** — reads `predictor/metrics/drift_{date}.json`
- **Store vs Inline Usage** — latest predictions metric; full metric is in CloudWatch logs
- **Recent Snapshots** — last 14 days of feature snapshots

---

## Page 6: Execution (`views/6_Execution.py`)

Trade history and slippage monitoring. Merges the former Trade Log and Slippage pages.

A recent-activity summary sits above two tabs — **Trade Log** and **Slippage Monitor**.

### Trade Log tab

Full audit trail of every order placed.

#### Filters

| Filter | Type |
|--------|------|
| Date range | Date input (start/end) |
| Action | Multiselect: ENTER / EXIT / REDUCE |
| Ticker | Text input |
| Market regime | Multiselect |
| Min score | Slider (0–100) |

#### Trade Table

Paginated at 25 rows/page. Columns from `trades_full.csv`:

`Date · Ticker · Action · Shares · Price · Fill Price · NAV at Order · Position % · Score · Conviction · Rating · Sector Rating · Regime · Upside · IB Order ID`

Download button exports filtered view as CSV.

#### Trade Summary Stats

Aggregated from filtered rows:
- Total ENTER / EXIT / REDUCE counts
- Avg score at ENTER
- Most common regime at ENTER
- Most active sectors (top 3)
- Avg position size % NAV

#### Outcome Join

For ENTER trades with a matching `score_performance` row (symbol + date):
- Shows `beat_spy_10d` and `beat_spy_30d` inline
- ✅ beat SPY / ❌ did not / ⏳ outcome pending

### Slippage Monitor tab

Execution quality by comparing `price_at_order` vs `fill_price`. Positive slippage = unfavorable (normalized across buy/sell directions).

- **Summary metrics**: trade count with fill data, mean / median / P95 slippage (bps), % unfavorable
- **Distribution histogram** with a zero reference line
- **By action** stats table (mean / median / std / count per action)
- **By market regime** stats table
- **Daily mean slippage** line chart over time
- **Worst 20 slippage events** table

Gracefully shows an info banner when `fill_price` / `price_at_order` columns are missing (executor has not yet run with fill confirmation).

---

## Page 7: Predictor (`views/7_Predictor.py`)

Answers: _is the model healthy, and what is it predicting today?_

### Model Health Banner

Model version, last trained date, training sample count from `predictor/metrics/latest.json`. Status badge: 🟢 Healthy / 🟡 Degraded / 🔴 Stale. Four metric cards:

| Card | Source |
|------|--------|
| Hit Rate (30d rolling) | `hit_rate_30d_rolling` |
| IC (30d) | `ic_30d` |
| IC IR (30d) | `ic_ir_30d` |
| High-confidence predictions today | `n_high_confidence` |

### Today's Predictions Table

Full universe from `predictions/latest.json`, sorted by `p_up - p_down` descending. Default filter: high-confidence only (≥ 0.65); toggle to show all.

| Column | Source |
|--------|--------|
| Ticker | |
| Direction | UP ↑ / FLAT → / DOWN ↓ with row color |
| Confidence | `prediction_confidence` |
| P(UP) / P(FLAT) / P(DOWN) | Raw softmax probabilities |
| Score modifier | Points applied to technical score (`±` value or `—` if gate not met) |
| Current rating | From today's signals.json |

### Model Performance Trend

Rolling hit rate chart from `predictor_outcomes` (requires ≥60 resolved predictions). Source: `charts/predictor_chart.make_model_drift_chart`.

### Model Mode History

Weekly IC by model type (MSE / Lambdarank / Ensemble) with star markers on the selected mode per training run. Source: `predictor/metrics/mode_history.json`.

### Feature Importance

SHAP-based feature importance bar chart with IC overlay, plus noise candidate list. Source: `predictor/metrics/feature_importance.json`.

### Prediction History — Ticker Drilldown

Selectbox: any ticker in `predictor_outcomes`. Charts:
- Line: `p_up - p_down` over time (net directional signal, range −1 to +1)
- Outcome markers on resolution date: ✅ correct / ❌ wrong
- Running accuracy: `X correct of Y predictions (Z%)`

### Hit Rate by Confidence Bucket

Grouped bar chart with 0.65–0.75, 0.75–0.85, 0.85–1.0 buckets showing hit rate and sample count. Validates that confidence is monotonically predictive. Moved here from the former Signal Quality page in Phase 6. Requires ≥20 resolved predictions.

### Confidence Calibration Chart

Scatter: x = `prediction_confidence` decile, y = actual hit rate within that decile. A well-calibrated model produces a near-diagonal line. Meaningful after ~100 resolved predictions; shows calibration banner until then.

### Prediction vs. Signal Disagreements

Table of tickers where predictor direction conflicts with composite score signal — e.g., ENTER signal but DOWN prediction, or EXIT signal but UP prediction. These are the highest-tension cases for manual review.

| Column | Source |
|--------|--------|
| Ticker | |
| Signal | ENTER / EXIT / HOLD |
| Score | Composite score |
| Predicted Direction | UP / DOWN / FLAT |
| Confidence | |
| Outcome | ✅/❌/⏳ if resolved in `score_performance` |
