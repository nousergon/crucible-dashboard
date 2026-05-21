## PFE short-sell — accidental short on a long-only system

**Date:** 2026-04-22 · **Severity:** P1 · **Detection-to-fix:** same day

The executor opened a short position on a stock the system was only supposed to be exiting. A long-only system cannot end up short by design — so this was a real bug that needed both a live patch and a structural fix the same session.

### Symptoms

End-of-day reconciliation flagged a position with negative share count on a name that was being closed out. The trade log showed a SELL order had filled twice for what should have been a single exit, putting the position 76 shares below zero.

### Detection

EOD reconciliation surfaced the negative share count automatically — the reconcile path computes share-level deltas and a negative result is an invariant violation, not a warning. The issue was visible the same evening it occurred.

### Root cause

The exit retry path didn't account for the broker's `PreSubmitted` order state. When the first SELL was sitting in `PreSubmitted` (queued, not yet acknowledged at the venue), the retry treated the order as not-yet-submitted and fired a second SELL of the full position size. By the time IB acknowledged the first SELL it had already filled — the second SELL then filled against the now-empty position and went short.

### Fix

Three independent layers, any one of which would have prevented the bad fill:

1. The retry path now treats `PreSubmitted` orders as in-flight — no second submission until the first either fills or is rejected.
2. The position sizer caps SELL quantity at `held - in_flight_sell` so even a buggy retry can't oversell the actual position.
3. An auto-cover resolver runs at the start of every session, detects any negative position, and flattens it via market-on-open buy.

### Systemic improvement

Defense-in-depth was the deliberate shape — three layers covering the same bug class from three different angles. The auto-cover resolver in particular catches not just this specific bug but any future variant of "system somehow ended up short," regardless of how it got there.
