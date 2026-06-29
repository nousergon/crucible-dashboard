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
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from loaders.changelog_loader import _entry_to_row
from loaders.s3_loader import _research_bucket, get_s3_client

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
