"""Which model graded an eval record (alpha-engine-config-I11484).

Pure helpers, no I/O and no dashboard imports, so the loader, the
judge-bias component and the Eval Quality view share one definition and
the component stays importable in tests without the S3 loader.

``judge_model`` on an eval record is the judge's stable TIER key
(``claude-haiku-4-5`` / ``claude-sonnet-4-6``): it names the S3 key, the
CloudWatch dimension and the rolling-mean series, and it is NOT the
grader. Since alpha-engine-config-I2997 the sync rung's tiers are served
by whatever the router's ``low`` group resolves to; on the 2026-10-10
weekly, 81 of 81 records (74 first-pass, 7 escalation) were served by the
same model for both tiers. The judge writes what served each call, read
from the router's response, as ``judge_resolved_model``
(crucible-research#832).
"""

from __future__ import annotations

from typing import Any, Mapping


def served_grader(artifact: Mapping[str, Any]) -> str | None:
    """The model that actually graded ``artifact``, or None if unrecorded.

    Returns None, never the tier key, when ``judge_resolved_model`` is
    absent or blank: a record that does not say who graded it must not be
    shown as graded by the model its tier is named after.
    """
    served = artifact.get("judge_resolved_model")
    if isinstance(served, str) and served.strip():
        return served.strip()
    return None


def grader_label(judge_tier: str | None, served: str | None) -> str:
    """Display/grouping label for a grader: the served model, or an
    explicit "unrecorded" marker carrying the tier (never the bare tier)."""
    if served:
        return served
    return f"unrecorded (tier {judge_tier or '—'})"
