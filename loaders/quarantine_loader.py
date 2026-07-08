"""Loader for quarantined changelog entries (`changelog/quarantine/`).

Vocab-validation violators are routed to
``s3://{research_bucket}/changelog/quarantine/{YYYY-MM-DD}/{event_id}.json``
instead of ``changelog/entries/`` by both producers that guard the corpus:

- the auto-emit Lambdas (nousergon-data ``infrastructure/lambdas/_shared/vocab.py``), and
- the ``append-changelog`` composite action (nousergon-docs, config#863).

A quarantined object is a normal schema-1.0.0 changelog entry with an added
``validation_errors`` array naming each vocab field that fell outside its
allowed set. Keeping the corpus + retro-mining filter (the /Changelog page)
on conforming entries only means rejects are invisible there — this loader
backs the /Quarantine triage page (config#868) so an operator can see what
was rejected and why, and decide whether a reject was a genuine typo or a
schema lag worth migrating into ``entries/``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from loaders.changelog_loader import _ENTRY_PREFIX, _entry_to_row
from loaders.s3_loader import _research_bucket, get_s3_client

logger = logging.getLogger(__name__)

_QUARANTINE_PREFIX = "changelog/quarantine"

# Quarantine rows reuse the changelog field mapping (a quarantine entry IS a
# changelog entry) plus the quarantine-only ``validation_errors`` reason. Kept
# explicit so a schema growth doesn't silently widen the table.
_QUARANTINE_COLUMNS = [
    "ts_utc",
    "event_type",
    "severity",
    "subsystem",
    "root_cause_category",
    "summary",
    "actor",
    "source",
    "validation_errors",
    "event_id",
]


def _quarantine_to_row(entry: dict) -> dict:
    """Map a quarantined entry to a flat row.

    Reuses :func:`changelog_loader._entry_to_row` for the shared schema-1.0.0
    fields and joins the quarantine-only ``validation_errors`` array into a
    readable string. Tolerates a missing/non-list ``validation_errors`` (an
    entry that reached quarantine some other way) without raising.
    """
    base = _entry_to_row(entry)
    errors = entry.get("validation_errors") or []
    if isinstance(errors, list):
        joined = "; ".join(str(e) for e in errors)
    else:
        joined = str(errors)
    return {
        "ts_utc": base["ts_utc"],
        "event_type": base["event_type"],
        "severity": base["severity"],
        "subsystem": base["subsystem"],
        "root_cause_category": base["root_cause_category"],
        "summary": base["summary"],
        "actor": base["actor"],
        "source": base["source"],
        "validation_errors": joined,
        "event_id": base["event_id"],
    }


@st.cache_data(ttl=900)
def load_quarantine_entries(days: int = 30) -> pd.DataFrame:
    """Read quarantined entries from the last ``days`` days into a DataFrame.

    Lists per-day prefixes (bounded, cheap) and reads each JSON object.
    Best-effort: a missing day prefix or an unparseable object is skipped, so
    a single bad entry never blanks the page. Empty DataFrame (with the
    expected columns) when there is nothing to triage — the common case, since
    quarantine only fills on a malformed vocab override.
    """
    bucket = _research_bucket()
    client = get_s3_client()
    rows: list[dict] = []
    today = date.today()

    for offset in range(max(days, 1)):
        day = (today - timedelta(days=offset)).isoformat()
        prefix = f"{_QUARANTINE_PREFIX}/{day}/"
        token: str | None = None
        while True:
            kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                resp = client.list_objects_v2(**kwargs)
            except Exception:
                break  # day prefix missing / transient — skip this day
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                try:
                    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                    rows.append(_quarantine_to_row(json.loads(body)))
                except Exception:
                    continue  # unparseable / unreadable — skip this entry
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break

    df = pd.DataFrame(rows, columns=_QUARANTINE_COLUMNS)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts_utc"], errors="coerce", utc=True)
        df["day"] = df["ts"].dt.date
        df = df.sort_values("ts", ascending=False).reset_index(drop=True)
    return df


# ── write path — "Approve & migrate to entries/" (config#868) ──────────────
#
# Requires the dashboard role to hold s3:PutObject/DeleteObject on both
# `changelog/entries/*` and `changelog/quarantine/*` — codified in
# alpha-engine-config `iam/alpha-engine-dashboard-role/
# alpha-engine-dashboard-changelog-quarantine-writeback.json`, NOT yet
# applied live as of this change. Until an operator runs the `aws iam
# put-role-policy` in that file's README, calls here 403 — expected, and
# surfaced to the operator as a clear error rather than a page crash.


def migrate_quarantine_entry(day: str, event_id: str) -> tuple[bool, str]:
    """Copy a quarantined entry to `changelog/entries/` then delete the
    quarantine copy. Returns ``(ok, message)``; never raises.

    Low-volume, operator-triggered admin action — a plain read → PutObject
    → DeleteObject sequence (copy+delete) is sufficient; no transactional
    guarantees beyond "the entries/ write is confirmed before the
    quarantine object is removed" (so a mid-sequence failure leaves the
    quarantine copy in place rather than silently dropping the entry).
    """
    bucket = _research_bucket()
    client = get_s3_client()
    src_key = f"{_QUARANTINE_PREFIX}/{day}/{event_id}.json"
    dst_key = f"{_ENTRY_PREFIX}/{day}/{event_id}.json"

    try:
        body = client.get_object(Bucket=bucket, Key=src_key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — classified into a clear message below
        logger.warning("[quarantine] read failed for %s: %s", src_key, exc)
        return False, f"could not read quarantine entry {src_key}: {exc}"

    try:
        entry = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[quarantine] unparseable entry %s: %s", src_key, exc)
        return False, f"quarantine entry {src_key} is not valid JSON: {exc}"

    try:
        client.put_object(
            Bucket=bucket,
            Key=dst_key,
            Body=json.dumps(entry, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to operator, not raised
        logger.warning("[quarantine] migrate write failed for %s: %s", dst_key, exc)
        return False, (
            f"write to {dst_key} failed ({exc}). The IAM grant for this "
            "action may not be applied live yet — see "
            "alpha-engine-config iam/alpha-engine-dashboard-role/"
            "alpha-engine-dashboard-changelog-quarantine-writeback.json. "
            "The quarantine entry was left in place."
        )

    try:
        client.delete_object(Bucket=bucket, Key=src_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[quarantine] cleanup delete failed for %s: %s", src_key, exc)
        return False, (
            f"migrated to {dst_key} but failed to delete {src_key} ({exc}) — "
            "the entry now exists in both places; delete the quarantine "
            "copy manually."
        )

    return True, f"migrated to {dst_key}"
