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
from loaders.observation_registry_loader import load_observation_registry



HEARTBEAT_KEY = "_freshness_monitor/heartbeat.json"
CHECK_RESULTS_KEY = "_freshness_monitor/check_results.json"
HISTORY_KEY = "_freshness_monitor/history.json"

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


@st.cache_data(ttl=300)
def _load_history() -> dict | None:
    """Per-artifact historical-cycle probe results from the daily 04:00
    UTC historical run. Higher TTL than check_results because the
    underlying data refreshes once/day, not every 15min."""
    return _fetch_s3_json(_research_bucket(), HISTORY_KEY)


def _format_age(iso_ts) -> str:
    """Render an age string from an ISO-8601 timestamp.

    Accepts ``str | None`` semantically, but defensively type-checks the
    input — when fed through ``DataFrame.apply()`` with the PyArrow
    backend, JSON null values arrive as ``pd.NA`` (not Python ``None``),
    which is truthy under ``not iso_ts`` for some dtype paths AND fails
    ``datetime.fromisoformat`` with ``TypeError`` rather than
    ``ValueError``. Surfaced 2026-05-28 after the freshness-monitor
    Phase 6 bootstrap landed live ``check_results.json`` with 49/51
    null ``last_modified`` values (grace_period entries that hadn't
    been probed yet).
    """
    if not isinstance(iso_ts, str) or not iso_ts:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
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


# ── Type derivation — observation vs production ─────────────────────────────
#
# Surfaces which artifacts exist for observe-mode rollouts (parallel-observe,
# soak-gated cutover) vs which are load-bearing for production. Derived from
# the cross-link in OBSERVATION_REGISTRY.yaml (composes_with) + the artifact's
# severity. Rule:
#
#   - production: severity == critical (load-bearing for trading, regardless
#     of whether an observation entry references it), OR no active
#     observation references this artifact (severity=warning entries are
#     secondary observability for production runs).
#   - observation: severity == warning AND at least one active observation
#     entry (state != gated-off) lists this artifact in composes_with.
#
# Composes with feedback_observe_mode_unconditional_gates_govern_cutover —
# this column makes the production/observation distinction visible on the
# freshness surface, complementing /Active_Observations (page 27).
@st.cache_data(ttl=60)
def _load_active_observation_artifact_refs() -> set[str]:
    """Return the set of artifact_ids referenced by ACTIVE (state in
    {always-on, gated-on}) observation entries' composes_with. None
    return if the registry can't be loaded — treat as empty set."""
    reg = load_observation_registry()
    if reg is None:
        return set()
    active_states = {"always-on", "gated-on"}
    refs: set[str] = set()
    for obs in reg.get("observations", []) or []:
        if obs.get("state") not in active_states:
            continue
        for ref in obs.get("composes_with") or []:
            if isinstance(ref, str):
                refs.add(ref)
    return refs


def _derive_type(severity: str, artifact_id: str, observation_refs: set[str]) -> str:
    """Apply the production-vs-observation rule. Severity wins (critical
    is always production); cross-link decides for warning entries."""
    if severity == "critical":
        return "production"
    if artifact_id in observation_refs:
        return "observation"
    return "production"


_observation_refs = _load_active_observation_artifact_refs()
df["type"] = df.apply(
    lambda r: _derive_type(r.get("severity", ""), r.get("artifact_id", ""), _observation_refs),
    axis=1,
)


# Filters — operator-facing slice-and-dice.
filter_cols = st.columns(5)
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
with filter_cols[4]:
    types_present = sorted(df["type"].dropna().unique().tolist())
    selected_types = st.multiselect(
        "Type",
        types_present,
        default=types_present,
        help=(
            "production = load-bearing for trading or a production "
            "run's secondary observability. observation = artifact "
            "emitted for an observe-mode rollout that hasn't been "
            "cut over yet. See /Active_Observations for full context."
        ),
    )

filtered = df[
    df["owner_repo"].isin(selected_owners)
    & df["cadence"].isin(selected_cadences)
    & df["severity"].isin(selected_severities)
    & df["state"].isin(selected_states)
    & df["type"].isin(selected_types)
].copy()

if filtered.empty:
    st.info("No rows match the current filters.")
    st.stop()

# Display: state badge + key columns sorted by severity-first then state.
filtered["state_display"] = filtered["state"].map(_STATE_LABEL).fillna(filtered["state"])
filtered["last_modified_age"] = filtered["last_modified"].apply(_format_age)

# Historical gap-count column — derived from the daily history.json
# probe (alpha-engine-data freshness-monitor historical mode).
# ✅ continuous, ⚠️ N gaps, — if history not yet covered for this id
# (e.g. a continuous-cadence artifact, which historical mode skips).
history_payload = _load_history()
_history_artifacts = (history_payload or {}).get("artifacts", {})


def _format_history_summary(artifact_id: str) -> str:
    entry = _history_artifacts.get(artifact_id)
    if entry is None:
        return "—"
    if entry.get("is_latest_pointer"):
        # latest-pointer artifacts don't have a meaningful gap count
        # (single point); show present/absent state only.
        h = entry.get("history") or []
        if h and h[0].get("present"):
            return "✅ exists (latest-pointer)"
        return "❌ absent (latest-pointer)"
    gap_count = entry.get("gap_count")
    lookback = entry.get("lookback_cycles", 0)
    if gap_count is None or lookback == 0:
        return "—"
    if gap_count == 0:
        return f"✅ {lookback}/{lookback} continuous"
    return f"⚠️ {gap_count}/{lookback} gaps"


filtered["history_summary"] = filtered["artifact_id"].apply(_format_history_summary)

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
    "type",
    "artifact_id",
    "owner_repo",
    "cadence",
    "severity",
    "canonical_key",
    "last_modified_age",
    "history_summary",
    "sla_violated_by_minutes",
    "recovery_substituted",
    "reason",
]
display_df = filtered[display_cols].rename(columns={
    "state_display": "State",
    "type": "Type",
    "artifact_id": "Artifact",
    "owner_repo": "Owner",
    "cadence": "Cadence",
    "severity": "Severity",
    "canonical_key": "S3 Key",
    "last_modified_age": "Last Modified",
    "history_summary": "History (12wk)",
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


# ── Historical-cycle per-artifact drill-down ────────────────────────────────
#
# Reads _freshness_monitor/history.json (written daily at 04:00 UTC by
# the freshness-monitor Lambda's historical mode). Shows the per-cycle
# history for any artifact whose summary on the main table reads
# something other than ✅ continuous — i.e., the artifacts that need
# attention. Latest-pointer artifacts surface their current state only.
#
# Calendar-naive — NYSE holidays may render as false-positive ❌ absent
# cells; operator interprets in context. Calendar-aware probe is a
# future enhancement.


_filtered_history_artifacts = {
    aid: _history_artifacts[aid]
    for aid in filtered["artifact_id"].tolist()
    if aid in _history_artifacts
}

if history_payload is None:
    st.info(
        "No historical probe data yet. The daily 04:00 UTC EB cron "
        "(`alpha-engine-freshness-monitor-historical-cron`) writes "
        f"`s3://{_research_bucket()}/{HISTORY_KEY}`. First firing "
        "lands tomorrow; manual invoke: "
        "`aws lambda invoke --function-name alpha-engine-freshness-monitor "
        "--payload '{\"mode\":\"historical\"}' --cli-binary-format raw-in-base64-out /tmp/out.json`."
    )
elif _filtered_history_artifacts:
    st.subheader("Per-artifact history drill-down")
    st.caption(
        f"Generated {history_payload.get('generated_at', '?')}. "
        f"Lookback: {history_payload.get('lookback', {})}. "
        "Click any artifact below to see the per-cycle sequence."
    )

    # Surface gappy + latest-pointer-absent artifacts first; expand the
    # worst offenders by default.
    def _drill_sort_key(item):
        aid, entry = item
        if entry.get("is_latest_pointer"):
            # latest-pointer absent → high priority; present → low
            present = (entry.get("history") or [{}])[0].get("present")
            return (0 if not present else 9, aid)
        gap = entry.get("gap_count") or 0
        if gap == 0:
            return (8, aid)  # continuous → low priority
        return (1, -gap, aid)  # gappy → high priority, most gaps first

    sorted_history = sorted(
        _filtered_history_artifacts.items(),
        key=_drill_sort_key,
    )

    # Auto-expand the first 3 worst-offender entries.
    for idx, (aid, entry) in enumerate(sorted_history):
        cadence = entry.get("cadence", "?")
        if entry.get("is_latest_pointer"):
            h = entry.get("history") or []
            present = h[0].get("present") if h else None
            badge = "✅ exists" if present else "❌ absent"
            label = f"{aid}  ({cadence}, latest-pointer, {badge})"
        else:
            gap = entry.get("gap_count", 0)
            lookback = entry.get("lookback_cycles", 0)
            if gap == 0:
                badge = f"✅ {lookback}/{lookback} continuous"
            else:
                badge = f"⚠️ {gap}/{lookback} gaps"
            label = f"{aid}  ({cadence}, {badge})"

        with st.expander(label, expanded=(idx < 3)):
            rows = []
            for c in entry.get("history") or []:
                rows.append({
                    "date": c.get("date"),
                    "present": "✅" if c.get("present") else "❌",
                    "size": c.get("size", ""),
                    "last_modified": c.get("last_modified", ""),
                    "error_code": c.get("error_code", ""),
                })
            if rows:
                hist_df = pd.DataFrame(rows)
                st.dataframe(hist_df, use_container_width=True, hide_index=True)
            else:
                st.caption(
                    f"No history cycles probed (cadence={cadence!r} — "
                    "continuous artifacts are skipped by historical mode; "
                    "current-state probe covers them)."
                )
            st.caption(
                f"S3 key template: `{entry.get('s3_key_template', '?')}`  •  "
                f"severity: `{entry.get('severity', '?')}`  •  "
                f"owner: `{entry.get('owner_repo', '?')}`"
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
