"""
fleet_status_loader.py — input gathering for the Fleet Status page.

Gathers one :class:`fleet_status.FleetInputs` snapshot per cache window
(25 s TTL — the page's ``st.fragment(run_every="30s")`` tick always lands
on a fresh read) from the planes the resolver composes:

- AWS control plane: ``ec2:DescribeInstances`` + SSM agent ``PingStatus``
  via ``ssm:DescribeInstanceInformation`` — the authority for "is the box
  up" (config#1724 doctrine: independent signal over self-report). These
  need the ``alpha-engine-dashboard-fleet-liveness`` inline policy
  (alpha-engine-config ``iam/alpha-engine-dashboard-role/``); until it is
  applied the snapshot degrades to ``ec2_available=False`` and the page
  renders a named warning banner — never a silent gray.
- Step Functions: reuses ``loaders.pipeline_status_loader`` (page 25's
  substrate — same 60 s cache, same last-good S3 fallback), condensed to
  :class:`fleet_status.PipelineSnapshot`.
- S3 artifacts: freshness-monitor heartbeat/check_results, the daemon's
  ``intraday/nav.json`` LastModified age, groom in-progress marker + the
  newest run artifact, ``health/{module}.json`` self-reports.
- Local box: ``systemctl is-active nous-ergon-live`` (unit-file presence
  gates the probe so off-box dev renders gray, not red).

Per ``feedback_no_silent_fails``: every unavailable plane is carried as a
typed field on FleetInputs (``ec2_available``/``ec2_error``, pipeline
``UNAVAILABLE`` snapshots, ``None`` probes) and surfaced by the page —
degraded reads are visible, never swallowed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import date, datetime, timezone

import streamlit as st

from fleet_status import (
    FleetInputs,
    GroomSnapshot,
    ModuleHealthRow,
    PipelineSnapshot,
)
from loaders.pipeline_status_loader import (
    LoadOutcome,
    derive_cycle_verdict,
    read_pipeline_state_with_fallback,
)
from loaders.s3_loader import (
    _research_bucket,
    _trades_bucket,
    download_s3_json,
    get_s3_client,
    list_groom_run_keys,
    load_groom_run,
)
from trading_calendar import is_trading_day

logger = logging.getLogger(__name__)

_TTL_SECONDS = 25

# Instance IDs: env-overridable with the fleet's live defaults (same
# pattern as alpha-engine-data/infrastructure/lambdas/eod-backstop).
TRADING_INSTANCE_ID = os.environ.get("TRADING_INSTANCE_ID", "i-018eb3307a21329bf")
# Name tag the groom EC2 spot launches under (config groom_run infra).
GROOM_SPOT_NAME = os.environ.get("GROOM_SPOT_NAME", "alpha-engine-groom-spot")

_HEARTBEAT_KEY = "_freshness_monitor/heartbeat.json"
_CHECK_RESULTS_KEY = "_freshness_monitor/check_results.json"
_INTRADAY_NAV_KEY = "intraday/nav.json"
_GROOM_IN_PROGRESS_KEY = "groom/in_progress.json"
_LIVE_UNIT_FILE = "/etc/systemd/system/nous-ergon-live.service"

# SF ARNs — mirrors views/25_Pipeline_Status.py + canonical role filters
# (Option-D: cadence runs, not smoke/recovery overlays).
_REGION = "us-east-1"
_ACCOUNT_ID = "711398986525"
_PIPELINES = {
    "weekly": ("ne-weekly-freshness-pipeline", "weekly"),
    "preopen": ("ne-preopen-trading-pipeline", "daily"),
    "postclose": ("ne-postclose-trading-pipeline", "eod"),
}


def _arn_for(sf_name: str) -> str:
    return f"arn:aws:states:{_REGION}:{_ACCOUNT_ID}:stateMachine:{sf_name}"


def _parse_iso(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── AWS control plane ───────────────────────────────────────────────────────


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _ec2_snapshot() -> dict:
    """{"available": bool, "error": str|None, "state": str|None, "ping": str|None}

    ``state`` is the trading instance's EC2 state; ``ping`` its SSM agent
    PingStatus (Online / ConnectionLost / Inactive). A PingStatus read
    failure degrades to ping=None without hiding a good EC2 read —
    instance state is the primary signal, agent ping the wedge detector.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    out: dict = {"available": True, "error": None, "state": None, "ping": None,
                 "groom_spot_running": False, "groom_spot_launched_at": None}
    try:
        ec2 = boto3.client("ec2", region_name=_REGION)
        resp = ec2.describe_instances(InstanceIds=[TRADING_INSTANCE_ID])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                out["state"] = (inst.get("State") or {}).get("Name")
    except (ClientError, BotoCoreError) as exc:
        logger.warning("fleet_status ec2 describe failed: %s", exc)
        return {"available": False, "error": f"{type(exc).__name__}: {exc}",
                "state": None, "ping": None,
                "groom_spot_running": False, "groom_spot_launched_at": None}

    try:
        # Independent "groomer running now" signal: a live groom spot box.
        # Covers runs the S3 in-progress marker can't (a run launched on
        # pre-marker driver code, or a driver that died before finalizing).
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [GROOM_SPOT_NAME]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                out["groom_spot_running"] = True
                lt = inst.get("LaunchTime")
                if lt is not None:
                    out["groom_spot_launched_at"] = lt.isoformat()
    except (ClientError, BotoCoreError) as exc:
        # Secondary signal — WARN + carry on; the groomer row falls back to
        # the marker/recency tiers and the trading-instance read stands.
        logger.warning("fleet_status groom-spot describe failed: %s", exc)

    try:
        ssm = boto3.client("ssm", region_name=_REGION)
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [TRADING_INSTANCE_ID]}]
        )
        infos = resp.get("InstanceInformationList", [])
        if infos:
            out["ping"] = infos[0].get("PingStatus")
    except (ClientError, BotoCoreError) as exc:
        # Secondary signal only — WARN + carry on with ping=None; the page
        # still shows EC2 state and the reason string says ping is unknown.
        logger.warning("fleet_status ssm describe failed: %s", exc)
    return out


# ── Step Functions ──────────────────────────────────────────────────────────


def _pipeline_snapshots() -> dict[str, PipelineSnapshot]:
    """Condense page-25's loader results (already 60s-cached + S3-fallback)."""
    snaps: dict[str, PipelineSnapshot] = {}
    for key, (sf_name, role) in _PIPELINES.items():
        result = read_pipeline_state_with_fallback(
            _arn_for(sf_name), role_filter={role}
        )
        if result.outcome == LoadOutcome.NO_EXECUTIONS:
            snaps[key] = PipelineSnapshot(status="NO_EXECUTIONS")
            continue
        if result.run is None:
            snaps[key] = PipelineSnapshot(
                status="UNAVAILABLE", error=result.error_message
            )
            continue
        run = result.run
        verdict = derive_cycle_verdict(run)
        current = next(
            (t.state_name for t in run.tasks if t.status.value == "RUNNING"), None
        )
        error = (
            None
            if result.outcome in (LoadOutcome.LIVE, LoadOutcome.LIVE_ROLE_FALLBACK)
            else result.error_message
        )
        snaps[key] = PipelineSnapshot(
            status=run.status.value,
            verdict=verdict.verdict,
            started_at=run.start_utc,
            stopped_at=run.end_utc,
            current_state=current,
            error=error,
        )
    return snaps


# ── S3 artifacts ────────────────────────────────────────────────────────────


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _intraday_nav_age_s() -> float | None:
    """Age (s) of the daemon's intraday/nav.json heartbeat, None if absent."""
    try:
        client = get_s3_client()
        head = client.head_object(Bucket=_research_bucket(), Key=_INTRADAY_NAV_KEY)
        lm = head.get("LastModified")
        if lm is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - lm).total_seconds())
    except Exception as exc:  # noqa: BLE001 — 404 (no heartbeat yet) and read
        # errors both mean "no usable heartbeat"; the resolver renders the
        # honest red/gray and the S3 client layer logs the specifics.
        logger.info("intraday nav head failed (absent or unreadable): %s", exc)
        return None


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _freshness_artifacts() -> tuple[dict | None, dict | None]:
    hb = download_s3_json(_research_bucket(), _HEARTBEAT_KEY)
    cr = download_s3_json(_research_bucket(), _CHECK_RESULTS_KEY)
    return (hb if isinstance(hb, dict) else None, cr if isinstance(cr, dict) else None)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _groom_snapshot_raw() -> dict:
    """Marker + newest-run fields, JSON-able for st.cache_data."""
    out: dict = {
        "marker_started_at": None, "marker_tier": None, "marker_model": None,
        "last_run_start": None, "last_stop_reason": None, "last_model": None,
    }
    marker = download_s3_json(_research_bucket(), _GROOM_IN_PROGRESS_KEY)
    # Marker contract (groom_driver.py): {"active": bool, "run_start": iso,
    # "tier": str, "model": str}. active=False = finalized leftover.
    # Absent marker (pre-marker driver, or key never written) ⇒ recency tiers.
    if isinstance(marker, dict) and marker.get("active"):
        out["marker_started_at"] = marker.get("run_start")
        out["marker_tier"] = marker.get("tier") or marker.get("issue_filter")
        out["marker_model"] = marker.get("model")
    keys = list_groom_run_keys(limit=1)
    if keys:
        run = load_groom_run(keys[0])
        if run:
            out["last_run_start"] = run.get("run_start")
            out["last_stop_reason"] = run.get("stop_reason")
            out["last_model"] = run.get("model")
    return out


def _groom_snapshot(ec2: dict) -> GroomSnapshot:
    raw = _groom_snapshot_raw()
    return GroomSnapshot(
        marker_started_at=_parse_iso(raw["marker_started_at"]),
        marker_tier=raw["marker_tier"],
        marker_model=raw["marker_model"],
        last_run_start=_parse_iso(raw["last_run_start"]),
        last_stop_reason=raw["last_stop_reason"],
        last_model=raw["last_model"],
        spot_running=bool(ec2.get("groom_spot_running")),
        spot_launched_at=_parse_iso(ec2.get("groom_spot_launched_at")),
    )


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _module_health_rows() -> list[dict]:
    """health/{module}.json self-reports — module list derived from lib
    (config#1728: never a hand-kept copy)."""
    from nousergon_lib.health import DASHBOARD_HEALTH_MODULES

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for module_name, bucket_key, _stale_after in DASHBOARD_HEALTH_MODULES:
        bucket = _research_bucket() if bucket_key == "research" else _trades_bucket()
        health = download_s3_json(bucket, f"health/{module_name}.json")
        if not isinstance(health, dict):
            rows.append({"module": module_name, "status": "unknown",
                         "age_hrs": None, "error": None})
            continue
        age_hrs = None
        last_dt = _parse_iso(health.get("last_success"))
        if last_dt is not None:
            age_hrs = (now - last_dt).total_seconds() / 3600
        rows.append({
            "module": module_name,
            "status": health.get("status", "unknown"),
            "age_hrs": age_hrs,
            "error": health.get("error"),
        })
    return rows


# ── Local box services ──────────────────────────────────────────────────────


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def _live_service_ok() -> bool | None:
    """systemd is the authority for the sibling live service; only probed
    where the unit exists (off-box dev ⇒ None ⇒ gray, never a false red)."""
    if not os.path.exists(_LIVE_UNIT_FILE):
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "nous-ergon-live"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip() == "active"
    except Exception as exc:  # noqa: BLE001 — probe failure on the box IS a
        # finding: report unhealthy rather than unknown.
        logger.warning("nous-ergon-live probe failed: %s", exc)
        return False


# ── Public API ──────────────────────────────────────────────────────────────


def gather_fleet_inputs() -> FleetInputs:
    """One coherent snapshot for fleet_status.resolve_fleet."""
    now = datetime.now(timezone.utc)
    ec2 = _ec2_snapshot()
    hb, cr = _freshness_artifacts()
    return FleetInputs(
        now=now,
        is_trading_day=is_trading_day(date.today()),
        ec2_available=ec2["available"],
        ec2_error=ec2["error"],
        trading_instance_state=ec2["state"],
        trading_instance_ping=ec2["ping"],
        live_service_ok=_live_service_ok(),
        intraday_nav_age_s=_intraday_nav_age_s(),
        pipelines=_pipeline_snapshots(),
        heartbeat=hb,
        check_results=cr,
        groom=_groom_snapshot(ec2),
        module_health=tuple(ModuleHealthRow(**r) for r in _module_health_rows()),
    )
