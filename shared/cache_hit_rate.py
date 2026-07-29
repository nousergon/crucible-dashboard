"""Cache-hit-rate computation over the LLM cost telemetry stream.

Gate **G6** of ``nous-ergon-ops/policies/prompt-caching-policy.md``. Caching is
the largest controllable cost lever the fleet has — at agentic-loop shape
~90–98% of billed input tokens are re-reads of an identical prefix — and the
easiest thing to break silently, because a cache miss is behaviourally
identical to a hit: same output, no error, no log line. This module turns the
per-call cost records into the rate that makes a prompt-shape regression
visible before the invoice does.

**The denominator is mechanism-dependent, and the two are not interchangeable.**
That is the whole reason this is a module rather than three inline lines:

``reported_miss``
    Automatic-prefix providers (DeepSeek, Moonshot, Zhipu) report hit and miss
    as their own pair. ``prompt_cache_miss_tokens`` is the denominator's other
    half; ``input_tokens`` there is the TOTAL and would understate the miss.

``uncached_input``
    On Anthropic (explicit ``cache_control`` breakpoints) ``input_tokens``
    already IS the uncached remainder, so the rate is computable with no miss
    field at all.

``unknown``
    Everything else. Reported as coverage, never imputed.

Two failure modes this module exists to avoid, both of which produce a
confidently wrong number rather than an error:

1. **Filling an absent ``prompt_cache_miss_tokens`` with zero.** The field is
   additive (krepis 0.19.2); every partition written before it carries no such
   column. Zero-filling collapses the denominator onto the cache-read count
   and renders a **100% hit rate for all historical rows**.
2. **Reading a zero denominator as a 0% hit rate.** Zero means "this provider
   reported nothing", not "nothing hit the cache".
"""
from __future__ import annotations

import pandas as pd

# Anthropic models are the explicit-breakpoint (M1) mechanism. Keyed on the
# model name rather than a ``provider`` column because
# ``krepis.cost.record_anthropic_call`` — the Anthropic-only entry point —
# does not emit ``provider`` at all, while ``record_llm_call`` does. The model
# name is present on every row from both writers, so it is the only field that
# classifies reliably across mixed-vintage partitions.
ANTHROPIC_MODEL_PREFIXES = ("claude-",)

BASIS_REPORTED_MISS = "reported_miss"
BASIS_UNCACHED_INPUT = "uncached_input"
BASIS_UNKNOWN = "unknown"


def cache_basis(model: object, prompt_cache_miss_tokens: object) -> str:
    """Which denominator is valid for one row's hit rate, if any.

    A provider-reported miss count wins over the model-name heuristic: it is a
    direct statement from the provider, whereas the prefix match is an
    inference about which mechanism the model uses.
    """
    if prompt_cache_miss_tokens is not None and pd.notna(prompt_cache_miss_tokens):
        return BASIS_REPORTED_MISS
    if str(model or "").startswith(ANTHROPIC_MODEL_PREFIXES):
        return BASIS_UNCACHED_INPUT
    return BASIS_UNKNOWN


def with_cache_denominator(frame: pd.DataFrame) -> pd.DataFrame:
    """Return *frame* plus ``cache_basis`` and ``cache_denominator`` columns.

    Never mutates the input. Rows whose basis is ``unknown`` get a zero
    denominator and MUST be excluded by the caller rather than counted as
    misses — see :func:`hit_rate`.
    """
    out = frame.copy()
    if "prompt_cache_miss_tokens" not in out.columns:
        # Absent column, not absent values: a pre-0.19.2 partition. Introduce
        # it as NA so every row classifies as `unknown` rather than as a
        # perfect hit rate.
        out["prompt_cache_miss_tokens"] = pd.NA
    for col in ("cache_read_tokens", "input_tokens"):
        if col not in out.columns:
            out[col] = 0
        out[col] = out[col].fillna(0)

    out["cache_basis"] = [
        cache_basis(m, miss)
        for m, miss in zip(out.get("model", pd.Series([None] * len(out))),
                           out["prompt_cache_miss_tokens"])
    ]

    out["cache_denominator"] = 0.0
    reported = out["cache_basis"] == BASIS_REPORTED_MISS
    if reported.any():
        out.loc[reported, "cache_denominator"] = (
            out.loc[reported, "cache_read_tokens"].astype(float)
            + out.loc[reported, "prompt_cache_miss_tokens"].fillna(0).astype(float)
        )
    uncached = out["cache_basis"] == BASIS_UNCACHED_INPUT
    if uncached.any():
        out.loc[uncached, "cache_denominator"] = (
            out.loc[uncached, "cache_read_tokens"].astype(float)
            + out.loc[uncached, "input_tokens"].astype(float)
        )
    return out


def measurable(frame: pd.DataFrame) -> pd.DataFrame:
    """The subset of *frame* whose hit rate is actually computable."""
    return frame[
        (frame["cache_basis"] != BASIS_UNKNOWN) & (frame["cache_denominator"] > 0)
    ]


def hit_rate(frame: pd.DataFrame) -> float | None:
    """Aggregate hit rate over *frame*, or ``None`` when unmeasurable.

    ``None`` rather than ``0.0`` deliberately: a window with no cache
    telemetry has an *unknown* hit rate, and rendering 0% would read as a
    total cache failure — the opposite of the truth in the common case, where
    the provider simply did not report.
    """
    known = measurable(frame)
    denom = float(known["cache_denominator"].sum())
    if not denom:
        return None
    return float(known["cache_read_tokens"].sum()) / denom


def by_model(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-model hit rate over the measurable rows, widest denominator first."""
    known = measurable(frame)
    if known.empty:
        return pd.DataFrame(
            columns=["model", "cache_read", "denominator", "calls", "cost_usd", "hit_rate"]
        )
    agg = {
        "cache_read": ("cache_read_tokens", "sum"),
        "denominator": ("cache_denominator", "sum"),
        "calls": ("cache_read_tokens", "size"),
    }
    if "cost_usd" in known.columns:
        agg["cost_usd"] = ("cost_usd", "sum")
    out = known.groupby("model", as_index=False).agg(**agg)
    out["hit_rate"] = out["cache_read"] / out["denominator"]
    return out.sort_values("denominator", ascending=False)
