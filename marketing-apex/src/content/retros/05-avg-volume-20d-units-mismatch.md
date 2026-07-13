---
title: 'avg_volume_20d units mismatch — 901 of 903 tickers silently failed a liquidity gate'
date: '2026-05-25'
severity: 'P1'
domain: 'Data contracts'
order: 5
summary: >-
  A feature column was written as a normalized ratio and read as raw share
  volume. Nothing crashed for months — the scanner just quietly rejected
  almost the entire universe. This is the incident that started the
  units-suffix naming contract.
---

A liquidity gate meant to filter out illiquid names was instead filtering out almost everything — 901 of 903 tickers — for months, with no error, no alert, and no crash. The bug wasn't a wrong calculation; it was two correct implementations that silently disagreed about what a number meant.

**Date:** found 2026-05-25 · **Severity:** P1 · **Resolution:** same-day fix, multi-PR arc across two repos

### Symptoms

Production scanner runs were producing a small handful of candidates per weekly cycle instead of the several dozen the system's own design docs anticipated. Nothing failed — the pipeline ran green every week. It just quietly wasn't finding much.

### Detection

Surfaced during a smoke check on an unrelated feature-store change, not by an alert: someone noticed the scanner's liquidity gate pass-count looked implausibly low for a universe of ~900 liquid, large-cap-adjacent names, and pulled the thread.

### Root cause

`avg_volume_20d` was computed and written by the data layer as a normalized ratio (values clustering near 1.0) — the correct representation for the predictor's feature vector. The scanner's liquidity gate, in a different module, read that same column expecting raw share volume and compared it against a five-figure minimum-shares threshold. A ratio near 1.0 compared against a raw-shares floor fails that comparison for almost every ticker, every time — silently, because a numeric comparison that evaluates to "too illiquid" looks exactly like a correct rejection. Two consumers agreed on a column name and disagreed on its unit, and nothing in the pipeline was positioned to notice.

### Fix

An additive, non-breaking split rather than a rename: a new `avg_volume_20d_raw` column carries the raw-shares value the scanner actually needs, alongside the existing normalized column the predictor already depends on — so the predictor's feature vector and any live model weights needed no retrain. The scanner was pointed at the new raw column. Landed alongside a declarative feature-store schema contract and a set of schema-contract tests that pin column ↔ catalog ↔ documentation agreement, plus a source-level consumer-contract test asserting the scanner reads raw units specifically.

### Systemic improvement

Every feature-store column now carries a mandatory units suffix — `_raw`, `_ratio`, `_pct`, `_zscore`, or `_log_return` — enforced by a schema-contract test that fails CI on any new bare-named column. The unit is now part of the name, not an assumption two modules have to independently get right and never verify against each other. This incident is the reference case for why that contract exists.
