"""
sla_status_loader.py — input gathering for the Fleet SLA console page
(config#2858).

Gathers one :class:`sla_status.SlaInputs` snapshot per cache window
(25 s TTL — same as ``fleet_status_loader.py``) from the three planes
the resolver composes:

- ``ARTIFACT_REGISTRY.yaml`` mirrored to S3 by the config repo's
  ``sync-artifact-registry.yml`` workflow on every push
  (``_freshness_monitor/ARTIFACT_REGISTRY.yaml``) — the process/SLA
  definitions. Parsed the same way the freshness-monitor Lambda parses
  it (``defaults`` merged into every entry), condensed to
  :class:`sla_status.SlaRegistryRow`.
- ``_freshness_monitor/check_results.json`` — reused verbatim (same key
  ``views/26_Artifact_Freshness.py`` reads).
- ``_freshness_monitor/history.json`` — reused verbatim (same key).

Per ``feedback_no_silent_fails``: an unreadable registry/check_results/
history artifact degrades to an empty/None field rather than raising —
the page renders the honest "no data" state (mirrors
``fleet_status_loader.py``'s degrade-and-surface posture).
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from loaders.s3_loader import _research_bucket, download_s3_json, download_s3_yaml
from sla_status import SlaInputs, SlaRegistryRow

_TTL_SECONDS = 25

_REGISTRY_KEY = "_freshness_monitor/ARTIFACT_REGISTRY.yaml"
_CHECK_RESULTS_KEY = "_freshness_monitor/check_results.json"
_HISTORY_KEY = "_freshness_monitor/history.json"

# Cadences the resolver understands; any other value in the registry is
# carried through as-is (resolve_process falls back to NOT_EXPECTED via
# its cadence-lookup dict.get default — no row is silently dropped).
_KNOWN_CADENCES = {"saturday_sf", "weekday_sf", "eod_sf", "continuous"}


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _load_registry_rows() -> list[dict]:
    """Parse ARTIFACT_REGISTRY.yaml into plain dicts (JSON-able for
    st.cache_data) with ``defaults`` merged in, mirroring the
    freshness-monitor Lambda's own ``load_registry`` merge semantics."""
    raw = download_s3_yaml(_research_bucket(), _REGISTRY_KEY)
    if not isinstance(raw, dict):
        return []
    defaults = raw.get("defaults") or {}
    rows = []
    for entry in raw.get("artifacts") or []:
        if not isinstance(entry, dict) or "artifact_id" not in entry:
            continue
        merged = {**defaults, **entry}
        rows.append(merged)
    return rows


def _registry_rows() -> tuple[SlaRegistryRow, ...]:
    out = []
    for row in _load_registry_rows():
        cadence = row.get("cadence")
        sla = row.get("sla_minutes_after_cron")
        if cadence is None or sla is None:
            # Malformed row (schema is CI-guarded upstream by
            # test_artifact_registry_schema.py; this is defense-in-depth,
            # not the enforcement point) — skip rather than crash the page.
            continue
        out.append(
            SlaRegistryRow(
                artifact_id=row["artifact_id"],
                cadence=cadence,
                sla_minutes_after_cron=int(sla),
                owner_repo=row.get("owner_repo") or "?",
                severity=row.get("severity") or "warning",
            )
        )
    return tuple(out)


def gather_sla_inputs() -> SlaInputs:
    """One coherent snapshot for sla_status.resolve_sla_table."""
    now = datetime.now(timezone.utc)
    check_results = download_s3_json(_research_bucket(), _CHECK_RESULTS_KEY)
    history = download_s3_json(_research_bucket(), _HISTORY_KEY)
    return SlaInputs(
        now=now,
        registry=_registry_rows(),
        check_results=check_results if isinstance(check_results, dict) else None,
        history=history if isinstance(history, dict) else None,
    )
