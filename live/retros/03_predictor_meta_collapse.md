## Predictor meta-model collapse — 27 UP / 0 DOWN

**Date:** 2026-04-28 · **Severity:** P1 · **Resolution:** 5-PR arc, same week

A weekly retrain produced a meta-learner with a degenerate output distribution. Catching it before any trading decisions used the bad model — and finding the structural cause rather than symptom-patching — illustrates the kind of "loud failure beats silent drift" discipline this system is built for.

### Symptoms

Morning predictor email showed 27 UP predictions and 0 DOWN predictions across the universe. Confidence values clustered into a narrow band with only a few unique values. A healthy distribution looks roughly balanced with continuous confidence values across the full range.

### Detection

The morning briefing email itself was the detector — the distribution shape was sharply different from the typical run. Caught before the executor used the predictions for sizing or veto decisions, so no trading impact.

### Root cause

The Layer-2 Ridge meta-learner combines outputs from Layer-1 specialized models with research-context features (research composite score, conviction signal, sector macro modifier) sourced from the research module's `signals.json`. The research module was emitting these values correctly — the bug was on the *consumption* side. During an earlier scaffold pass inside the predictor's meta-trainer, these features had been hardcoded to placeholder *constant* values in the walk-forward training loop, with the intent to swap in the real per-ticker reads from `signals.json` once that data path was finalized. The data path landed but the swap was missed.

With constant inputs, Ridge correctly assigned zero coefficients to those features — a healthy regularizer doing exactly what it was supposed to do. The meta-learner then effectively reduced to its remaining Layer-1 inputs, producing a much narrower decision surface than it was designed to.

### Fix

Five-PR arc, all on `alpha-engine-predictor` — no changes to the research module or to `signals.json`:

- Replace the hardcoded constants in `meta_trainer.py` with real per-ticker reads from `signals.json` for the walk-forward training loop
- Streaming refactor for memory efficiency (an OOM issue surfaced as a side-effect during the fix; closed in the same arc rather than papered over)
- Three retraining attempts, each gated on a named-baseline check before promotion

The final promoted model showed the research-context coefficients non-zero — the structural diagnostic that the upstream wiring was now load-bearing.

### Systemic improvement

Validation IC moved from 0.053 to 0.132 — a 2.48× improvement on the predictive substrate the executor relies on. Beyond the immediate fix, the arc surfaced a class of bug — *placeholder constants in production training data* — that drove a follow-up audit pass for similar dead-input patterns elsewhere in the training pipelines.
