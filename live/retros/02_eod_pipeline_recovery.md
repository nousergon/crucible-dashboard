## EOD pipeline recovery — 50 minutes to 4.5 minutes

**Date:** 2026-05-01 · **Severity:** P2 · **Resolution:** same day, 3 PRs

The end-of-day Step Function was running for ~50 minutes against a typical 5-minute envelope. Three independent issues had stacked. Closing them the same day cut the runtime by an order of magnitude and propagated a defensive pattern to other parts of the data layer.

### Symptoms

EOD Step Function runtime ballooned from ~5 minutes to ~50 minutes. The pipeline still completed (the upper-bound timeout hadn't fired), but the runtime was eating into the next day's preparation window and the runtime curve in CloudWatch was visibly off-trend.

### Detection

Runtime trend in the SF execution history made the regression obvious — typical runs sit in a tight ~3–5 minute band, and a 10× outlier is both unmistakable visually and triggers a runtime alarm.

### Root cause

Three independent issues stacked into one user-visible failure:

1. The historical price backfill path was emitting NaN-filled VWAP rows that downstream calculations were silently absorbing as outliers, dragging out the column-wise statistics step.
2. The daily-append step had no skip-if-exists guard — re-running it on a date that was already written rewrote the same rows from scratch instead of no-op'ing, wasting minutes per re-run.
3. The EOD SSM script was carrying obsolete logic from a prior architecture that no longer needed to run.

None of the three would have shown up as a single failure mode. They compounded — and when they did, the pipeline was slow but not broken, which is the worst kind of regression because nothing screams.

### Fix

Three same-day PRs in `alpha-engine-data`:

- VWAP NaN-fill rule on the backfill path — explicit, documented, with a regression test
- `skip_if_exists` guard on `daily_append` so re-runs are idempotent at the day-level
- EOD SSM script trimmed to current responsibilities

### Systemic improvement

Runtime: 50 minutes → 4.5 minutes, an 11× reduction. The skip-if-exists pattern was generalized as the standing convention for any append-style write into the data layer — preventing this class of "slow not broken" regression from emerging elsewhere.
