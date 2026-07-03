"""Morning Signal content-schedule loader/writer (console producer side).

The console's "Content Schedule" page (``views/45_Morning_Signal_Schedule.py``)
edits the per-date schedule manifest the morning-signal generator consumes at
generation time (``morning_signal.schedule_override``):

    s3://morning-signal-podcast/schedule/schedule.json      (the manifest)
    s3://morning-signal-podcast/schedule/applied/…          (generator-written
                                                             "aired" markers)

This is a cross-repo PRODUCT CONTRACT (schema v1): the dependency-free
:func:`validate_schedule_manifest` and the fixtures under
``tests/fixtures/schedule/`` are duplicated IDENTICALLY in
``morning-signal`` (``src/morning_signal/schedule_override.py``); each repo's
contract test runs its copy over the shared fixtures so drift fails CI on
whichever side moved. Documentation JSON Schema lives in morning-signal's
``docs/schedule-schema.json``.

Write discipline (mirrors ``eval_loader.save_calibration_review``): writers
validate up front, stamp UTC timestamps, return ``(ok, message)`` and NEVER
raise. Lost-update protection uses S3 conditional writes (``IfMatch`` etag /
``IfNoneMatch="*"`` on create — botocore ≥ ~1.36); on an older botocore the
put falls back to a read-compare-then-unconditional-put with a documented
benign race (single-operator console). A 412 PreconditionFailed surfaces as
``(False, "conflict")`` so the page can reload instead of clobbering.

IAM: the box role ``alpha-engine-dashboard-role`` holds Get/Put on
``schedule/*`` + prefix-scoped ListBucket only (alpha-engine-config
``iam/alpha-engine-dashboard-role/alpha-engine-dashboard-morning-signal-schedule.json``,
applied 2026-07-03). Deliberately NO DeleteObject — deletes are manifest
edits; applied markers are generator-owned.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import boto3
import streamlit as st

logger = logging.getLogger(__name__)

MS_BUCKET_DEFAULT = "morning-signal-podcast"
MS_REGION = "us-west-2"
SCHEDULE_KEY = "schedule/schedule.json"
APPLIED_PREFIX = "schedule/applied/"
SCHEMA_VERSION = 1

VALID_MODES = ("override", "extend", "skip")
VALID_EDITIONS = ("am", "pm")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ms_bucket() -> str:
    """Schedule bucket — config-overridable, defaults to the podcast bucket."""
    try:
        from loaders.s3_loader import load_config

        return (load_config().get("morning_signal") or {}).get(
            "bucket", MS_BUCKET_DEFAULT
        )
    except Exception:  # noqa: BLE001 — config miss must not break the page
        return MS_BUCKET_DEFAULT


def _ms_client():
    """S3 client pinned to the podcast bucket's region (instance role)."""
    return boto3.client("s3", region_name=MS_REGION)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── contract validator (IDENTICAL copy of morning-signal's) ─────────────────


def validate_schedule_manifest(doc: object) -> list[str]:
    """Validate a parsed schedule manifest; return human-readable errors.

    Empty list = valid. Dependency-free by design (no ``jsonschema``) and
    duplicated IDENTICALLY in the consumer
    (morning-signal ``src/morning_signal/schedule_override.py``) — the
    per-repo contract tests run both copies over the same fixture files,
    so a divergence fails CI on whichever side drifted.
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["manifest is not a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION} "
            f"(got {doc.get('schema_version')!r})"
        )
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        errors.append("entries missing or not a mapping")
        return errors
    for date_key, entry in entries.items():
        where = f"entries[{date_key!r}]"
        if not isinstance(date_key, str) or not _DATE_RE.match(date_key):
            errors.append(f"{where}: key is not a YYYY-MM-DD date")
        else:
            try:
                datetime.strptime(date_key, "%Y-%m-%d")
            except ValueError:
                errors.append(f"{where}: key is not a real calendar date")
        if not isinstance(entry, dict):
            errors.append(f"{where}: entry is not an object")
            continue
        mode = entry.get("mode")
        if mode not in VALID_MODES:
            errors.append(
                f"{where}: mode must be one of {list(VALID_MODES)} "
                f"(got {mode!r})"
            )
        topic = entry.get("topic")
        if mode == "skip":
            if topic is not None and not isinstance(topic, str):
                errors.append(f"{where}: topic must be a string when present")
        elif not isinstance(topic, str) or not topic.strip():
            errors.append(f"{where}: topic must be a non-empty string")
        guidance = entry.get("guidance")
        if guidance is not None and not isinstance(guidance, str):
            errors.append(f"{where}: guidance must be a string when present")
        editions = entry.get("editions")
        if editions is not None:
            if (
                not isinstance(editions, list)
                or not editions
                or any(e not in VALID_EDITIONS for e in editions)
            ):
                errors.append(
                    f"{where}: editions must be a non-empty subset of "
                    f"{list(VALID_EDITIONS)}"
                )
        keywords = entry.get("keywords")
        if keywords is not None:
            if not isinstance(keywords, list) or any(
                not isinstance(k, str) or not k.strip() for k in keywords
            ):
                errors.append(
                    f"{where}: keywords must be a list of non-empty strings"
                )
        min_searches = entry.get("min_searches")
        if min_searches is not None:
            if not isinstance(min_searches, int) or isinstance(
                min_searches, bool
            ) or min_searches < 1:
                errors.append(f"{where}: min_searches must be an integer >= 1")
    return errors


# ── read path ────────────────────────────────────────────────────────────────


def _empty_manifest() -> dict:
    return {"schema_version": SCHEMA_VERSION, "entries": {}}


def _fetch_schedule() -> tuple[dict, str | None, str | None]:
    """Uncached fetch → ``(manifest, etag, error)``. Never raises.

    A missing manifest (unseeded schedule) is NOT an error — returns an
    empty manifest with ``etag=None`` (the save path then creates it with
    ``IfNoneMatch="*"``). Any other failure returns the empty manifest
    plus a human-readable ``error`` for the page banner.
    """
    try:
        client = _ms_client()
        resp = client.get_object(Bucket=_ms_bucket(), Key=SCHEDULE_KEY)
        etag = resp.get("ETag")
        manifest = json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — page must render regardless
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "NoSuchKey":
            return _empty_manifest(), None, None
        logger.warning("[ms_schedule] fetch failed: %s", exc)
        return _empty_manifest(), None, f"{type(exc).__name__}: {exc}"

    errors = validate_schedule_manifest(manifest)
    if errors:
        logger.warning("[ms_schedule] manifest invalid: %s", "; ".join(errors))
        return _empty_manifest(), etag, "manifest invalid: " + "; ".join(errors)
    return manifest, etag, None


@st.cache_data(ttl=60)
def load_schedule() -> tuple[dict, str | None, str | None]:
    """Cached ``(manifest, etag, error)`` for page renders (60s TTL —
    writes clear the cache immediately, so staleness only shows edits
    made outside this console)."""
    return _fetch_schedule()


@st.cache_data(ttl=300)
def load_applied_markers() -> dict[str, dict]:
    """Generator-written "aired" markers keyed ``{date}-{edition}``.

    Fail-soft ``{}`` — the badge is observability, never a page blocker.
    """
    markers: dict[str, dict] = {}
    try:
        client = _ms_client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_ms_bucket(), Prefix=APPLIED_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                stem = key[len(APPLIED_PREFIX):].removesuffix(".json")
                if not stem:
                    continue
                try:
                    body = client.get_object(Bucket=_ms_bucket(), Key=key)[
                        "Body"
                    ].read()
                    markers[stem] = json.loads(body)
                except Exception:  # noqa: BLE001 — skip one bad marker
                    logger.warning("[ms_schedule] unreadable marker %s", key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ms_schedule] applied-marker list failed: %s", exc)
        return {}
    return markers


# ── write path ───────────────────────────────────────────────────────────────


def save_schedule(manifest: dict, *, if_match: str | None) -> tuple[bool, str]:
    """Conditionally PUT the manifest. Returns ``(ok, message)``; never raises.

    ``if_match``: the etag the edit was based on (``None`` = the manifest
    didn't exist → create with ``IfNoneMatch="*"``). A concurrent edit
    surfaces as ``(False, "conflict")`` — reload and re-apply, don't clobber.
    On botocore too old for conditional writes (ParamValidationError), fall
    back to read-compare-then-unconditional-put: same protection minus a
    tiny compare-to-put race, acceptable for a single-operator console.
    """
    errors = validate_schedule_manifest(manifest)
    if errors:
        return False, "refusing to write invalid manifest: " + "; ".join(errors)

    manifest = dict(manifest)
    manifest["updated_at_utc"] = _utc_now()
    body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    def _put(**extra) -> tuple[bool, str]:
        _ms_client().put_object(
            Bucket=_ms_bucket(),
            Key=SCHEDULE_KEY,
            Body=body,
            ContentType="application/json",
            **extra,
        )
        return True, "saved"

    conditional = {"IfMatch": if_match} if if_match else {"IfNoneMatch": "*"}
    try:
        return _put(**conditional)
    except Exception as exc:  # noqa: BLE001 — classified below, never raised
        from botocore.exceptions import ClientError, ParamValidationError

        if isinstance(exc, ParamValidationError):
            # botocore predates S3 conditional writes — fallback path.
            logger.warning(
                "[ms_schedule] botocore lacks conditional writes; "
                "falling back to compare-then-put"
            )
            try:
                _, current_etag, _ = _fetch_schedule()
                if current_etag != if_match:
                    return False, "conflict"
                return _put()
            except Exception as exc2:  # noqa: BLE001
                logger.warning("[ms_schedule] fallback save failed: %s", exc2)
                return False, f"save failed: {exc2}"
        if isinstance(exc, ClientError) and exc.response.get("Error", {}).get(
            "Code"
        ) in ("PreconditionFailed", "412"):
            return False, "conflict"
        logger.warning("[ms_schedule] save failed: %s", exc)
        return False, f"save failed: {exc}"


def upsert_entry(date_str: str, entry: dict) -> tuple[bool, str]:
    """Fresh-fetch → set ``entries[date_str]`` → conditional save.

    Preserves ``created_at_utc`` on edit; stamps ``updated_at_utc``.
    """
    if not _DATE_RE.match(date_str or ""):
        return False, f"invalid date key {date_str!r}"
    manifest, etag, error = _fetch_schedule()
    if error:
        return False, f"cannot edit: schedule unreadable ({error})"
    entry = dict(entry)
    prior = manifest["entries"].get(date_str) or {}
    entry["created_at_utc"] = prior.get("created_at_utc") or _utc_now()
    entry["updated_at_utc"] = _utc_now()
    manifest["entries"][date_str] = entry
    return save_schedule(manifest, if_match=etag)


def delete_entry(date_str: str) -> tuple[bool, str]:
    """Fresh-fetch → remove ``entries[date_str]`` → conditional save."""
    manifest, etag, error = _fetch_schedule()
    if error:
        return False, f"cannot edit: schedule unreadable ({error})"
    if date_str not in manifest["entries"]:
        return False, f"no entry for {date_str}"
    del manifest["entries"][date_str]
    return save_schedule(manifest, if_match=etag)
