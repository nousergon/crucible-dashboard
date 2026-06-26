"""Loader for the unified changelog event-lake (`changelog/entries/`).

The changelog store is the fleet's single source for production-failure mining
(flow-doctor SOTA arc — alpha-engine-config#1273). Entries live at
``s3://{research_bucket}/changelog/entries/{YYYY-MM-DD}/{event_id}.json``
(schema 1.0.0), written by three feeders that all emit the same shape:

- **flow-doctor** s3 sink (rich captures from the big handlers — full
  ``flow_doctor`` block + diagnosis),
- **changelog-cloudwatch-mirror** (every Lambda's ERROR/CRITICAL/timeout),
- **changelog-incident-mirror** (SNS alerts).

This loader reads the last ``days`` of entries into a DataFrame for the
/Changelog mining page.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from loaders.s3_loader import _research_bucket, get_s3_client

_ENTRY_PREFIX = "changelog/entries"

# Columns surfaced to the page (top-level schema-1.0.0 fields + the most
# useful flow_doctor provenance). Kept explicit so a schema growth doesn't
# silently widen the table.
_COLUMNS = [
    "ts_utc",
    "event_type",
    "severity",
    "subsystem",
    "root_cause_category",
    "summary",
    "actor",
    "source",
    "error_signature",
    "dedup_count",
    "event_id",
]


def _entry_to_row(entry: dict) -> dict:
    fd = entry.get("flow_doctor") or {}
    return {
        "ts_utc": entry.get("ts_utc"),
        "event_type": entry.get("event_type"),
        "severity": entry.get("severity"),
        "subsystem": entry.get("subsystem"),
        "root_cause_category": entry.get("root_cause_category"),
        "summary": entry.get("summary"),
        "actor": entry.get("actor"),
        "source": entry.get("source"),
        "error_signature": fd.get("error_signature"),
        "dedup_count": fd.get("dedup_count"),
        "event_id": entry.get("event_id"),
    }


@st.cache_data(ttl=900)
def load_changelog_entries(days: int = 30) -> pd.DataFrame:
    """Read changelog entries from the last ``days`` days into a DataFrame.

    Lists per-day prefixes (bounded, cheap) and reads each JSON object.
    Best-effort: a missing day prefix or an unparseable object is skipped, so
    a single bad entry never blanks the page. Empty DataFrame (with the
    expected columns) when there is nothing to show.
    """
    bucket = _research_bucket()
    client = get_s3_client()
    rows: list[dict] = []
    today = date.today()

    for offset in range(max(days, 1)):
        day = (today - timedelta(days=offset)).isoformat()
        prefix = f"{_ENTRY_PREFIX}/{day}/"
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
                    rows.append(_entry_to_row(json.loads(body)))
                except Exception:
                    continue  # unparseable / unreadable — skip this entry
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break

    df = pd.DataFrame(rows, columns=_COLUMNS)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts_utc"], errors="coerce", utc=True)
        df["day"] = df["ts"].dt.date
        df = df.sort_values("ts", ascending=False).reset_index(drop=True)
    return df
