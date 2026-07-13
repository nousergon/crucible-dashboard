---
title: 'The false-positive trio — three RED flags, three broken estimators'
date: '2026-06-07'
severity: 'P2'
domain: 'Evaluation harness'
order: 6
summary: >-
  A single week produced three simultaneous critical RED flags on the system's
  own report card. All three turned out to be artifacts of how the metrics
  were estimated, not real problems — so the fix made the broken estimator
  classes impossible to construct at all.
---

Three metrics on the weekly report card went RED in the same week, each pointing at a different module. Investigating all three turned up the same underlying story three times: not a real regression, but a statistically naive estimator manufacturing a false alarm.

**Date:** 2026-06-07 · **Severity:** P2 · **Resolution:** same week, structural

### Symptoms

Three critical tiles on the weekly System Report Card flipped to RED in one cycle — enough simultaneous red flags that the honest first hypothesis was a real, systemic regression rather than three unrelated coincidences.

### Detection

The report card's own critical-gate rule is designed to escalate loudly rather than average bad news away, so all three RED flags surfaced clearly rather than getting buried in an otherwise-green weighted score. That visibility is what made it obvious these needed investigating together, not separately.

### Root cause

Each RED traced back to a different flawed estimator, not a different real failure:

- An all-or-nothing binary metric flipped its status because of a single noisy bucket, rather than reflecting any broader trend.
- A metric meant to evaluate a 21-day strategy horizon was actually being estimated from a 5-day proxy — the wrong measurement window entirely.
- A metric took the mean of an unbounded ratio, which a handful of outlier values were enough to blow far off any reasonable range.

None of the three represented a real problem in the system being measured. All three represented a problem in how the report card was measuring it.

### Fix

Rather than patch each metric individually, the three failure patterns were named as forbidden estimator classes for any critical metric: no strict all-or-nothing binaries, no measurement horizon shorter than the strategy horizon it's meant to evaluate, and no unbounded-ratio means without winsorizing or a robust alternative. Construction of a critical metric that violates any of these now raises before it can ever reach the report card — the bad estimator class can't be built, not just caught after the fact.

### Systemic improvement

The fix operates one layer up from the three specific metrics: any future critical metric, on any tile, inherits the same construction-time guardrail. A brittle estimator is now a build-time error for the person adding a metric, not a false alarm three weeks later for whoever's reading the card.
