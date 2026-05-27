"""
Artifact Freshness — Alpha Engine (private console)

Operator surface for the artifact-freshness-monitor arc (alpha-engine-lib
v0.40.0 substrate + alpha-engine-config ARTIFACT_REGISTRY.yaml SoT +
alpha-engine-data freshness-monitor Lambda, all shipped 2026-05-27).

The Lambda runs every 15min, walks the registry, and writes two
artifacts under ``s3://alpha-engine-research/_freshness_monitor/``:

  - ``heartbeat.json`` — last run timestamp + aggregate counts; the
    monitor monitors itself. Substrate-health-check daily watches the
    heartbeat's freshness.
  - ``check_results.json`` — per-spec result row (state, last-modified,
    SLA-breach minutes, reason). This page is the consumer-facing
    surface.

Per-artifact red/yellow/green at a glance, with filters by
``owner_repo`` / ``cadence`` / ``severity``. Companion KPI strip
appears on the System Health page (pages/4_System_Health.py) — this
page is the deep-dive surface.

**Plan doc:** ``~/Development/alpha-engine-docs/private/artifact-freshness-monitor-260527.md``
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import _fetch_s3_json, _research_bucket

st.set_page_config(
    page_title="Artifact Freshness — Alpha Engine",
    page_icon="📡",
    layout="wide",
)


HEARTBEAT_KEY = "_freshness_monitor/heartbeat.json"
CHECK_RESULTS_KEY = "_freshness_monitor/check_results.json"

# State → display color. Mirrors the substrate's CheckResult.state
# vocabulary. fresh = green; grace_period = muted green (not at-risk
# yet); stale/missing = amber/red per severity; probe_failed = red
# (the monitor itself is broken; operator must act).
_STATE_COLOR_HEX: dict[str, str] = {
    "fresh": "#1a7f37",
    "grace_period": "#5d9b6f",
    "stale": "#bf8700",
    "missing": "#cf222e",
    "probe_failed": "#82071e",
}

_STATE_LABEL: dict[str, str] = {
    "fresh": "✅ fresh",
    "grace_period": "⏳ grace",
    "stale": "⚠️ stale",
    "missing": "❌ missing",
    "probe_failed": "🚨 probe failed",
}


@st.cache_data(ttl=60)
def _load_heartbeat() -> dict | None:
    return _fetch_s3_json(_research_bucket(), HEARTBEAT_KEY)


@st.cache_data(ttl=60)
def _load_check_results() -> dict | None:
    return _fetch_s3_json(_research_bucket(), CHECK_RESULTS_KEY)


def _format_age(iso_ts: str | None) -> str:
    """Render an age string from an ISO-8601 timestamp."""
    if not iso_ts:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h ago"


def _style_state(val: str) -> str:
    color = _STATE_COLOR_HEX.get(val, "#57606a")
    return f"background-color: {color}; color: white; font-weight: 600;"


# ── Page header + heartbeat strip ───────────────────────────────────────────

st.title("📡 Artifact Freshness")
st.caption(
    "Absence-driven monitoring for load-bearing S3 artifacts. "
    "Complements flow-doctor / SF Catch (event-driven). "
    "Substrate: `alpha_engine_lib.artifact_freshness` (v0.40.0); "
    "SoT: `alpha-engine-config/private-docs/ARTIFACT_REGISTRY.yaml`."
)

heartbeat = _load_heartbeat()
if heartbeat is None:
    st.error(
        "No heartbeat artifact found at "
        f"`s3://{_research_bucket()}/{HEARTBEAT_KEY}`. "
        "Either the freshness-monitor Lambda has never run, or the "
        "Lambda's own writes are failing — investigate `aws lambda invoke "
        "--function-name alpha-engine-freshness-monitor`."
    )
    st.stop()

last_run = heartbeat.get("last_run")
counts = heartbeat.get("counts", {})
alerts_enabled = heartbeat.get("alerts_enabled", False)

# Top KPI strip — the at-a-glance state.
cols = st.columns([1.4, 1, 1, 1, 1, 1, 1.3])
with cols[0]:
    st.metric("Last run", _format_age(last_run))
with cols[1]:
    st.metric("✅ fresh", counts.get("fresh", 0))
with cols[2]:
    st.metric("⏳ grace", counts.get("grace_period", 0))
with cols[3]:
    st.metric("⚠️ stale", counts.get("stale", 0))
with cols[4]:
    st.metric("❌ missing", counts.get("missing", 0))
with cols[5]:
    st.metric("🚨 probe failed", counts.get("probe_failed", 0))
with cols[6]:
    mode_label = "🔔 ALERTS LIVE" if alerts_enabled else "👁 OBSERVE-only"
    st.metric("Mode", mode_label)

# Mode banner — OBSERVE state is operationally important context.
if not alerts_enabled:
    st.info(
        "**OBSERVE mode active.** Missing artifacts are surfaced here but "
        "no Telegram / SNS alerts fire. Phase 6 cutover flips this with "
        "`aws lambda update-function-configuration --function-name "
        "alpha-engine-freshness-monitor --environment "
        "'Variables={MNEMON_FRESHNESS_MONITOR_ENABLED=true,LOG_LEVEL=INFO}'`."
    )

st.divider()


# ── Detail table ────────────────────────────────────────────────────────────

check_results = _load_check_results()
if check_results is None or not check_results.get("results"):
    st.warning(
        "No per-spec check_results artifact found. Heartbeat exists "
        "but `check_results.json` is missing — Lambda may have crashed "
        "mid-pass after writing heartbeat but before writing results."
    )
    st.stop()

rows = check_results["results"]
df = pd.DataFrame(rows)

# Filters — operator-facing slice-and-dice.
filter_cols = st.columns(4)
with filter_cols[0]:
    owner_repos = sorted(df["owner_repo"].dropna().unique().tolist())
    selected_owners = st.multiselect(
        "Owner repo", owner_repos, default=owner_repos
    )
with filter_cols[1]:
    cadences = sorted(df["cadence"].dropna().unique().tolist())
    selected_cadences = st.multiselect("Cadence", cadences, default=cadences)
with filter_cols[2]:
    severities = sorted(df["severity"].dropna().unique().tolist())
    selected_severities = st.multiselect(
        "Severity", severities, default=severities
    )
with filter_cols[3]:
    states = sorted(df["state"].dropna().unique().tolist())
    selected_states = st.multiselect("State", states, default=states)

filtered = df[
    df["owner_repo"].isin(selected_owners)
    & df["cadence"].isin(selected_cadences)
    & df["severity"].isin(selected_severities)
    & df["state"].isin(selected_states)
].copy()

if filtered.empty:
    st.info("No rows match the current filters.")
    st.stop()

# Display: state badge + key columns sorted by severity-first then state.
filtered["state_display"] = filtered["state"].map(_STATE_LABEL).fillna(filtered["state"])
filtered["last_modified_age"] = filtered["last_modified"].apply(_format_age)

# Sort: probe_failed → missing → stale → grace → fresh, then severity desc.
_STATE_ORDER = {
    "probe_failed": 0,
    "missing": 1,
    "stale": 2,
    "grace_period": 3,
    "fresh": 4,
}
filtered["_sort_state"] = filtered["state"].map(_STATE_ORDER).fillna(99)
filtered["_sort_sev"] = filtered["severity"].map({"critical": 0, "warning": 1}).fillna(2)
filtered = filtered.sort_values(["_sort_state", "_sort_sev", "artifact_id"])

display_cols = [
    "state_display",
    "artifact_id",
    "owner_repo",
    "cadence",
    "severity",
    "canonical_key",
    "last_modified_age",
    "sla_violated_by_minutes",
    "recovery_substituted",
    "reason",
]
display_df = filtered[display_cols].rename(columns={
    "state_display": "State",
    "artifact_id": "Artifact",
    "owner_repo": "Owner",
    "cadence": "Cadence",
    "severity": "Severity",
    "canonical_key": "S3 Key",
    "last_modified_age": "Last Modified",
    "sla_violated_by_minutes": "SLA breach (min)",
    "recovery_substituted": "Recovery sub?",
    "reason": "Reason",
})

st.subheader(f"Per-artifact check results ({len(display_df)} of {len(df)} after filters)")

styled = display_df.style.map(_style_state, subset=["State"])
st.dataframe(
    styled,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Reason": st.column_config.TextColumn(width="large"),
        "S3 Key": st.column_config.TextColumn(width="medium"),
    },
)

st.divider()

# ── Footnotes / operator runbook ────────────────────────────────────────────

with st.expander("Operator runbook"):
    st.markdown(
        """
**When `probe_failed` count > 0** — the monitor itself is broken. Check:

```
aws logs tail /aws/lambda/alpha-engine-freshness-monitor --follow
```

Common causes: S3 bucket-policy change blocking the Lambda's role,
malformed registry row that the loader can't parse, transient S3
throttling. The Lambda's per-spec exception trap (`_check_one`)
isolates one bad row from sinking the rest of the pass.

**When `missing` count > 0** — a load-bearing artifact didn't arrive
by its SLA. Cross-check:

  - Did the upstream Step Function fire?
    `aws stepfunctions list-executions --state-machine-arn ...`
  - Is the producer failing silently?
    Search CloudWatch Logs for the producer Lambda / EC2 spot run.
  - Is this artifact still load-bearing? If retired, remove the row
    from `alpha-engine-config/private-docs/ARTIFACT_REGISTRY.yaml`
    and redeploy the Lambda.

**To force a fresh probe pass** (outside the 15min cron):

```
aws lambda invoke --function-name alpha-engine-freshness-monitor /tmp/r.json && cat /tmp/r.json
```

**Composes with:**

- `alpha-engine-data/infrastructure/lambdas/freshness-monitor/` — the Lambda
- `alpha-engine-config/private-docs/ARTIFACT_REGISTRY.yaml` — the SoT
- `alpha_engine_lib.artifact_freshness` — the substrate
- `alpha_engine_lib.alerts.publish` — the alert chokepoint
        """
    )
