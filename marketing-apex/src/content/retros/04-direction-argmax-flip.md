---
title: 'Direction argmax flip — the book flipped on zero new information'
date: '2026-07-06'
severity: 'P1'
domain: 'Model logic'
summary: >-
  Predicted direction was derived from a probability threshold that didn't
  match the label's true base rate, so ordinary day-to-day noise could flip
  almost the entire book. The fix was a one-line change to a single function.
---

The predictor's daily direction call flipped from 24 UP / 2 DOWN to 2 UP / 26 DOWN overnight, with no meaningful change in the underlying model scores. The bug wasn't in a model — it was in how a probability got turned into a direction.

**Date:** 2026-07-06 · **Severity:** P1 · **Detection-to-fix:** same day

### Symptoms

The daily direction tally — a standing sanity check on the predictor's output — swung from a heavily UP day to a heavily DOWN day back to back, with the underlying calibrated scores barely moving. A near-total book reversal with no corresponding shift in signal is itself the anomaly; nothing about the market or the model had changed enough to justify it.

### Detection

The direction counts are watched every morning as a standing distribution check, precisely because a healthy predictor's day-to-day direction mix should be reasonably stable. A 24:2 → 2:26 swing was visible immediately in that count, before it reached any position-sizing decision downstream.

### Root cause

Direction was derived by taking the argmax of a calibrated probability — effectively "UP if P(beat market) > 0.5, else DOWN." That threshold implicitly assumes the label is balanced around 50/50. It isn't: roughly 60% of names underperform SPY at the 21-day horizon the system targets, so the true zero-alpha crossing point sits away from p = 0.5. With the decision boundary planted at the wrong probability, small day-to-day recalibration noise near that boundary was enough to swing the argmax for most of the universe simultaneously — a book-wide reversal manufactured entirely by where the threshold was drawn, not by any change in the model's actual view.

### Fix

Direction is now derived directly from the sign of the predicted alpha itself — `sign(predicted_alpha)` — computed at a single `derive_direction` function and consumed at every site that needs a direction call. There is no longer a separate probability threshold to get wrong, and no FLAT state; the label is binary by construction, since "no opinion" was never actually a distinct trading thesis.

### Systemic improvement

Collapsing direction derivation to one function, one rule, and one call site closes off this entire class of bug: any future change to calibration or thresholding can't silently re-introduce a mismatched decision boundary, because there's no longer a decision boundary to place — the model's own sign is the answer.
