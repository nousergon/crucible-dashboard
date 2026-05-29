"""LLM-as-judge eval artifact loader (PR 4d, ROADMAP §1632-1633).

Loads ``decision_artifacts/_eval/{YYYY-MM-DD}/{judged_agent_id}/
{run_id}.{judge_model}.json`` artifacts from S3 and shapes them into
a long-format DataFrame the quality-trend page can pivot:

  | eval_date | judged_agent_id | criterion | score | judge_model |
  | rubric_id | rubric_version  | run_id    | overall_reasoning   |

The dashboard page reads the DataFrame directly. Per-page caching
(``@st.cache_data``) sits on this loader's public function rather
than the per-artifact fetches so the cache key is a (start, end)
date range — tight enough to update on demand but coarse enough that
flipping ticker filters in the UI doesn't re-fetch S3.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    _fetch_s3_json,
    _research_bucket,
    get_s3_client,
)

logger = logging.getLogger(__name__)


_EVAL_PREFIX = "decision_artifacts/_eval/"


def _list_eval_dates(bucket: str, *, max_days: int = 180) -> list[str]:
    """Return YYYY-MM-DD subprefix names under decision_artifacts/_eval/.

    Each subprefix corresponds to one eval-pipeline run date. Capped
    at ``max_days`` so the dashboard never fetches an unbounded
    history (CloudWatch metric retention is 15 months; the line-chart
    page rarely needs more than ~6 months of trailing data).
    """
    client = get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    dates: set[str] = set()
    try:
        for page in paginator.paginate(
            Bucket=bucket, Prefix=_EVAL_PREFIX, Delimiter="/",
        ):
            for cp in page.get("CommonPrefixes", []):
                # cp["Prefix"] looks like "decision_artifacts/_eval/2026-05-09/"
                trailing = cp["Prefix"][len(_EVAL_PREFIX):].rstrip("/")
                if len(trailing) == 10 and trailing.count("-") == 2:
                    dates.add(trailing)
    except Exception:  # noqa: BLE001
        logger.exception("[eval_loader] list eval dates failed")
        return []
    return sorted(dates)[-max_days:]


def _list_eval_keys_for_date(bucket: str, eval_date: str) -> list[str]:
    """Return every eval-artifact JSON key under one date partition."""
    client = get_s3_client()
    prefix = f"{_EVAL_PREFIX}{eval_date}/"
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".json"):
                    keys.append(key)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[eval_loader] list keys failed for date=%s", eval_date,
        )
    return keys


def _explode_eval_artifact(artifact: dict[str, Any], eval_date: str) -> list[dict]:
    """One row per (artifact, dimension) — long format for plotting."""
    rows: list[dict] = []
    judge_model = artifact.get("judge_model", "")
    judged_agent_id = artifact.get("judged_agent_id", "")
    rubric_id = artifact.get("rubric_id", "")
    rubric_version = artifact.get("rubric_version", "")
    run_id = artifact.get("run_id", "")
    overall = artifact.get("overall_reasoning", "")
    for dim in artifact.get("dimension_scores", []) or []:
        rows.append({
            "eval_date": eval_date,
            "judged_agent_id": judged_agent_id,
            "criterion": dim.get("dimension", ""),
            "score": dim.get("score"),
            "reasoning": dim.get("reasoning", ""),
            "judge_model": judge_model,
            "rubric_id": rubric_id,
            "rubric_version": rubric_version,
            "run_id": run_id,
            "overall_reasoning": overall,
        })
    return rows


@st.cache_data(ttl=900)
def load_eval_artifacts(
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    bucket: str | None = None,
) -> pd.DataFrame:
    """Load eval artifacts within ``[start_date, end_date]`` and return
    a long-format DataFrame.

    Defaults: ``end_date`` = today, ``start_date`` = end - 180 days.
    Returns an empty DataFrame with the expected schema when no eval
    artifacts have been written yet (first-run case during PR 4 deploy).
    """
    bkt = bucket or _research_bucket()
    end = end_date or date.today()
    start = start_date or (end - timedelta(days=180))

    all_dates = _list_eval_dates(bkt)
    in_window = [
        d for d in all_dates
        if start.isoformat() <= d <= end.isoformat()
    ]

    rows: list[dict] = []
    for d in in_window:
        for key in _list_eval_keys_for_date(bkt, d):
            artifact = _fetch_s3_json(bkt, key)
            if not artifact:
                continue
            rows.extend(_explode_eval_artifact(artifact, d))

    columns = [
        "eval_date", "judged_agent_id", "criterion", "score",
        "reasoning", "judge_model", "rubric_id", "rubric_version",
        "run_id", "overall_reasoning",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(rows, columns=columns)
    df["eval_date"] = pd.to_datetime(df["eval_date"])
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    return df.dropna(subset=["score"]).sort_values(
        ["eval_date", "judged_agent_id", "criterion", "judge_model"],
    ).reset_index(drop=True)


# ── Judge calibration review (ROADMAP L480 SOTA reframe) ──────────────────


_CALIBRATION_PREFIX = "decision_artifacts/_calibration/"


def _review_id(eval_date: str, judged_agent_id: str, run_id: str, judge_model: str) -> str:
    """Stable opaque key for one eval artifact under review.

    Used to dedupe — once an operator has reviewed an artifact, it
    drops out of the active-sampling queue.
    """
    return f"{eval_date}__{judged_agent_id}__{run_id}__{judge_model}"


def _score_uncertainty(dim_scores: list[dict] | None, rubric_midpoint: float = 3.0) -> float:
    """Lower = higher review priority.

    Active-sampling heuristic: rank artifacts by rubric-midpoint
    distance, mean-aggregated across dimensions. Scores nearest the
    midpoint carry the highest information value per minute of
    operator review — the LLM judge is most indecisive there.

    Returns ``+inf`` when the artifact has no scored dimensions
    (kicks the entry to the back of the queue rather than crashing
    the sort).
    """
    if not dim_scores:
        return float("inf")
    scores = [
        float(d.get("score"))
        for d in dim_scores
        if d.get("score") is not None
    ]
    if not scores:
        return float("inf")
    return sum(abs(s - rubric_midpoint) for s in scores) / len(scores)


@st.cache_data(ttl=300)
def load_recent_eval_artifacts_for_review(
    n: int = 10,
    *,
    bucket: str | None = None,
    lookback_days: int = 30,
    reviewed_ids: tuple[str, ...] | None = None,
) -> list[dict]:
    """Return up to ``n`` eval artifacts ranked by active-sampling
    priority (lowest band-midpoint distance first — see
    ``_score_uncertainty``). Stratified by (rubric_id, judged_agent_id)
    so coverage stays balanced.

    Excludes ``reviewed_ids`` so the queue self-shortens as the
    operator works through it.

    Loads the FULL artifact payload (not the long-format DataFrame
    used elsewhere) so the calibration UI can render per-dimension
    reasoning + overall_reasoning verbatim from what the judge saw.
    """
    bkt = bucket or _research_bucket()
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    reviewed = set(reviewed_ids or ())

    candidates: list[dict] = []
    for d in _list_eval_dates(bkt):
        if d < cutoff:
            continue
        for key in _list_eval_keys_for_date(bkt, d):
            artifact = _fetch_s3_json(bkt, key)
            if not artifact or not isinstance(artifact, dict):
                continue
            # Judge-skipped artifacts (Layer-1 structural skip in
            # `evals/judge.py`) carry no dimension scores — skip them.
            if artifact.get("judge_skip_reason"):
                continue
            rid = _review_id(
                d,
                artifact.get("judged_agent_id", ""),
                artifact.get("run_id", ""),
                artifact.get("judge_model", ""),
            )
            if rid in reviewed:
                continue
            artifact["_review_id"] = rid
            artifact["_eval_date"] = d
            artifact["_s3_key"] = key
            artifact["_uncertainty"] = _score_uncertainty(
                artifact.get("dimension_scores") or []
            )
            candidates.append(artifact)

    # Stratified pick: top-1 per (rubric_id, judged_agent_id) first,
    # then fill remaining slots by global uncertainty rank.
    candidates.sort(key=lambda a: a["_uncertainty"])
    seen: set[tuple[str, str]] = set()
    picked: list[dict] = []
    leftovers: list[dict] = []
    for a in candidates:
        stratum = (a.get("rubric_id", ""), a.get("judged_agent_id", ""))
        if stratum in seen:
            leftovers.append(a)
            continue
        seen.add(stratum)
        picked.append(a)
        if len(picked) >= n:
            break
    if len(picked) < n:
        picked.extend(leftovers[: n - len(picked)])
    return picked


def _list_reviewed_keys(bucket: str) -> list[str]:
    """Per-date append-only review archive keys under
    ``decision_artifacts/_calibration/{date}/reviews.jsonl``.
    """
    client = get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=_CALIBRATION_PREFIX):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if k.endswith("/reviews.jsonl"):
                    keys.append(k)
    except Exception:  # noqa: BLE001
        logger.exception("[eval_loader] list calibration keys failed")
    return keys


@st.cache_data(ttl=60)
def load_reviewed_ids(*, bucket: str | None = None) -> set[str]:
    """All ``review_id``s already submitted across the per-date JSONL
    archives. Short TTL so the UI's submit-then-refresh flow reflects
    the just-submitted record on the same render cycle.
    """
    import json

    bkt = bucket or _research_bucket()
    reviewed: set[str] = set()
    client = get_s3_client()
    for key in _list_reviewed_keys(bkt):
        try:
            obj = client.get_object(Bucket=bkt, Key=key)
            body = obj["Body"].read().decode("utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("[eval_loader] read failed for %s", key)
            continue
        for line in body.strip().split("\n"):
            if not line:
                continue
            try:
                rec = json.loads(line)
                rid = rec.get("review_id")
                if rid:
                    reviewed.add(rid)
            except Exception:  # noqa: BLE001
                pass  # tolerate corrupt lines; operator can re-review
    return reviewed


def save_calibration_review(review: dict, *, bucket: str | None = None) -> bool:
    """Append one review record to
    ``decision_artifacts/_calibration/{today}/reviews.jsonl``.

    Append semantics — read existing JSONL (or empty on miss), append
    the new line, re-upload. Auto-stamps ``reviewed_at_utc`` if absent.
    Returns True on success, False on any failure. Never raises.
    """
    import json
    from datetime import datetime, timezone

    if not isinstance(review, dict) or "review_id" not in review:
        logger.warning("[eval_loader] save_calibration_review rejected: missing review_id")
        return False

    bkt = bucket or _research_bucket()
    today = date.today().isoformat()
    key = f"{_CALIBRATION_PREFIX}{today}/reviews.jsonl"

    review.setdefault(
        "reviewed_at_utc",
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )

    try:
        client = get_s3_client()
        existing = b""
        try:
            obj = client.get_object(Bucket=bkt, Key=key)
            existing = obj["Body"].read()
        except Exception:  # noqa: BLE001 — treat as first write
            existing = b""
        new_line = (json.dumps(review, default=str) + "\n").encode("utf-8")
        client.put_object(
            Bucket=bkt,
            Key=key,
            Body=existing + new_line,
            ContentType="application/x-ndjson",
        )
        logger.info(
            "[eval_loader] wrote calibration review %s → s3://%s/%s",
            review.get("review_id"), bkt, key,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[eval_loader] save_calibration_review failed: %s", exc)
        return False


# ── Judge spot-check (ROADMAP L480 2026-05-29 re-scope) ───────────────────
#
# Read-only weekly transparency surface. Renders WHAT THE JUDGE SAW (the
# judged agent's output + input snapshot, hydrated via the eval
# artifact's ``judged_artifact_s3_key`` foreign key) beside WHAT THE
# JUDGE SAID (per-dimension scores + reasoning). No blind scoring — an
# eyeball pass, not a graded annotation. Optional 👍/👎 flags persist
# the rare "this judge call is wrong" exemplar for the outcome-IC study.
# Demotes blind-κ to optional per the 2026-05-29 re-scope.


_SPOTCHECK_PREFIX = "decision_artifacts/_spotcheck/"


@st.cache_data(ttl=300)
def load_recent_evals_for_spotcheck(
    n: int = 10,
    *,
    bucket: str | None = None,
    lookback_days: int = 30,
) -> list[dict]:
    """Return up to ``n`` recent eval artifacts for read-only spot-check,
    newest-date-first then ascending band-midpoint distance (borderline
    judge calls surface first within a date).

    Unlike the calibration queue this does NOT dedupe against a reviewed
    set — spot-check is a re-skimmable transparency pass, not a one-shot
    annotation. Judge-skipped artifacts (no dimension scores) are still
    excluded since there is no judge verdict to inspect.
    """
    bkt = bucket or _research_bucket()
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    out: list[dict] = []
    for d in sorted(_list_eval_dates(bkt), reverse=True):
        if d < cutoff:
            continue
        day_arts: list[dict] = []
        for key in _list_eval_keys_for_date(bkt, d):
            artifact = _fetch_s3_json(bkt, key)
            if not artifact or not isinstance(artifact, dict):
                continue
            if artifact.get("judge_skip_reason"):
                continue
            artifact["_review_id"] = _review_id(
                d,
                artifact.get("judged_agent_id", ""),
                artifact.get("run_id", ""),
                artifact.get("judge_model", ""),
            )
            artifact["_eval_date"] = d
            artifact["_s3_key"] = key
            artifact["_uncertainty"] = _score_uncertainty(
                artifact.get("dimension_scores") or []
            )
            day_arts.append(artifact)
        day_arts.sort(key=lambda a: a["_uncertainty"])
        out.extend(day_arts)
        if len(out) >= n:
            break
    return out[:n]


@st.cache_data(ttl=900)
def load_judged_artifact(
    s3_key: str | None, *, bucket: str | None = None,
) -> dict | None:
    """Hydrate the DecisionArtifact the judge scored, via the eval
    artifact's ``judged_artifact_s3_key`` foreign key.

    Returns the raw DecisionArtifact dict (``agent_output``,
    ``input_data_snapshot``, ``full_prompt_context`` …) or ``None`` when
    the key is absent / unfetchable. The foreign-key design (vs inlining
    the agent output into every eval artifact) is deliberate — this
    loader is the read-side of that contract, so a weekly spot-check can
    see everything the judge saw without bloating each eval artifact.
    """
    if not s3_key:
        return None
    bkt = bucket or _research_bucket()
    artifact = _fetch_s3_json(bkt, s3_key)
    if not artifact or not isinstance(artifact, dict):
        return None
    return artifact


def save_spotcheck_flag(flag: dict, *, bucket: str | None = None) -> bool:
    """Append one spot-check verdict to
    ``decision_artifacts/_spotcheck/{today}/flags.jsonl``.

    Lightweight companion to ``save_calibration_review`` — captures the
    rare "this judge call looks right/wrong" eyeball verdict (👍/👎 +
    optional note) as a flagged exemplar for the outcome-IC study.
    Auto-stamps ``flagged_at_utc``. Returns True on success, False on any
    failure. Never raises.
    """
    import json
    from datetime import datetime, timezone

    if not isinstance(flag, dict) or "spotcheck_id" not in flag:
        logger.warning("[eval_loader] save_spotcheck_flag rejected: missing spotcheck_id")
        return False

    bkt = bucket or _research_bucket()
    today = date.today().isoformat()
    key = f"{_SPOTCHECK_PREFIX}{today}/flags.jsonl"

    flag.setdefault(
        "flagged_at_utc",
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )

    try:
        client = get_s3_client()
        existing = b""
        try:
            obj = client.get_object(Bucket=bkt, Key=key)
            existing = obj["Body"].read()
        except Exception:  # noqa: BLE001 — treat as first write
            existing = b""
        new_line = (json.dumps(flag, default=str) + "\n").encode("utf-8")
        client.put_object(
            Bucket=bkt,
            Key=key,
            Body=existing + new_line,
            ContentType="application/x-ndjson",
        )
        logger.info(
            "[eval_loader] wrote spotcheck flag %s → s3://%s/%s",
            flag.get("spotcheck_id"), bkt, key,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[eval_loader] save_spotcheck_flag failed: %s", exc)
        return False
