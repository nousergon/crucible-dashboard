"""
54_Fleet_SLA.py — Fleet SLA / process-completion table (System & Ops,
config#2858).

Answers "did each scheduled process complete within its SLA, and what's
its track record?" — a table, not a dot-strip (Fleet Status already owns
the at-a-glance dots; this page is the SLA-accountability drill-down):
``process | pipeline | trigger | SLA | last completed | verdict | hit-rate``.

CONSUMES the freshness-monitor's three existing planes
(``ARTIFACT_REGISTRY.yaml``, ``check_results.json``, ``history.json``) —
it does not re-probe S3 or re-implement any monitor (config-I2861). Pure
verdict logic lives in ``sla_status.py`` (frozen-clock tested); input
gathering in ``loaders/sla_status_loader.py``.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from loaders.sla_status_loader import gather_sla_inputs
from sla_status import BREACHED, MET, NOT_EXPECTED, PENDING, resolve_sla_table

st.title("🎯 Fleet SLA")
st.caption(
    "Per-process SLA accountability, sourced from the freshness-monitor's "
    "own check_results.json (current cycle) + history.json (rolling "
    "hit-rate) — renders the existing monitoring planes, does not "
    "re-probe them. SoT: "
    "`alpha-engine-config/private-docs/ARTIFACT_REGISTRY.yaml`."
)

_VERDICT_LABEL = {
    MET: "✅ MET",
    BREACHED: "🔴 BREACHED",
    PENDING: "⏳ PENDING",
    NOT_EXPECTED: "⚪ NOT_EXPECTED",
}

_VERDICT_COLOR_HEX = {
    MET: "#1a7f37",
    BREACHED: "#cf222e",
    PENDING: "#bf8700",
    NOT_EXPECTED: "#57606a",
}


def _style_verdict(val: str) -> str:
    color = _VERDICT_COLOR_HEX.get(val, "#57606a")
    return f"background-color: {color}; color: white; font-weight: 600;"


def _format_age(ts) -> str:
    if ts is None:
        return "—"
    from datetime import datetime, timezone

    delta = datetime.now(timezone.utc) - ts
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h ago"


inputs = gather_sla_inputs()
rows = resolve_sla_table(inputs)

if not rows:
    st.warning(
        "No registry rows resolved — either "
        "`_freshness_monitor/ARTIFACT_REGISTRY.yaml` is missing/unparseable "
        "in S3 (check the config repo's `sync-artifact-registry.yml` "
        "workflow has run), or the registry itself is empty."
    )
    st.stop()

# ── KPI strip ────────────────────────────────────────────────────────────────
counts = {MET: 0, BREACHED: 0, PENDING: 0, NOT_EXPECTED: 0}
for r in rows:
    counts[r.verdict] = counts.get(r.verdict, 0) + 1

cols = st.columns(4)
with cols[0]:
    st.metric("✅ MET", counts[MET])
with cols[1]:
    st.metric("🔴 BREACHED", counts[BREACHED])
with cols[2]:
    st.metric("⏳ PENDING", counts[PENDING])
with cols[3]:
    st.metric("⚪ NOT_EXPECTED", counts[NOT_EXPECTED])

if inputs.check_results is None:
    st.info(
        "No `check_results.json` this snapshot — verdicts for cadenced "
        "rows fall back to a time-only PENDING/BREACHED judgment (never a "
        "guessed MET) until the freshness-monitor's next sweep lands."
    )

st.divider()

# ── Table ────────────────────────────────────────────────────────────────────
df = pd.DataFrame([
    {
        "process": r.process_id,
        "pipeline": r.pipeline,
        "trigger": r.trigger,
        "sla_minutes": r.sla_minutes_after_cron,
        "last_completed": _format_age(r.last_completed_utc),
        "verdict": r.verdict,
        "verdict_display": _VERDICT_LABEL.get(r.verdict, r.verdict),
        "hit_rate_30d": (
            f"{r.hit_rate_30d:.0%}" if r.hit_rate_30d is not None else "—"
        ),
        "lookback_cycles": r.lookback_cycles if r.lookback_cycles is not None else "—",
        "owner_repo": r.owner_repo or "?",
        "severity": r.severity or "?",
        "reason": (r.reason or "")[:120],
    }
    for r in rows
])

filter_cols = st.columns(4)
with filter_cols[0]:
    pipelines = sorted(df["pipeline"].dropna().unique().tolist())
    selected_pipelines = st.multiselect("Pipeline", pipelines, default=pipelines)
with filter_cols[1]:
    owners = sorted(df["owner_repo"].dropna().unique().tolist())
    selected_owners = st.multiselect("Owner repo", owners, default=owners)
with filter_cols[2]:
    severities = sorted(df["severity"].dropna().unique().tolist())
    selected_severities = st.multiselect("Severity", severities, default=severities)
with filter_cols[3]:
    verdicts = sorted(df["verdict"].dropna().unique().tolist())
    selected_verdicts = st.multiselect("Verdict", verdicts, default=verdicts)

filtered = df[
    df["pipeline"].isin(selected_pipelines)
    & df["owner_repo"].isin(selected_owners)
    & df["severity"].isin(selected_severities)
    & df["verdict"].isin(selected_verdicts)
].copy()

if filtered.empty:
    st.info("No rows match the current filters.")
    st.stop()

_VERDICT_SORT = {BREACHED: 0, PENDING: 1, NOT_EXPECTED: 2, MET: 3}
filtered["_sort_verdict"] = filtered["verdict"].map(_VERDICT_SORT).fillna(9)
filtered = filtered.sort_values(["pipeline", "_sort_verdict", "process"])

display_cols = [
    "verdict_display", "process", "pipeline", "trigger", "sla_minutes",
    "last_completed", "hit_rate_30d", "lookback_cycles", "owner_repo",
    "severity", "reason",
]
display_df = filtered[display_cols].rename(columns={
    "verdict_display": "Verdict",
    "process": "Process",
    "pipeline": "Pipeline",
    "trigger": "Trigger",
    "sla_minutes": "SLA (min)",
    "last_completed": "Last Completed",
    "hit_rate_30d": "Hit Rate",
    "lookback_cycles": "Lookback (cycles)",
    "owner_repo": "Owner",
    "severity": "Severity",
    "reason": "Reason",
})

st.subheader(f"Process SLA table ({len(display_df)} of {len(df)} after filters)")
styled = display_df.style.map(_style_verdict, subset=["Verdict"])
st.dataframe(
    styled,
    use_container_width=True,
    hide_index=True,
    column_config={"Reason": st.column_config.TextColumn(width="large")},
)

with st.expander("About this table"):
    st.markdown(
        """
**Verdict:**
- `MET` — the freshness monitor's own check_results.json marks this
  cycle `fresh` (or, for a not-yet-probed row, it landed before its SLA
  deadline).
- `BREACHED` — `stale` / `missing` / `probe_failed`, or a time-only
  fallback past the SLA deadline with no probe row yet.
- `PENDING` — `grace_period` (cold-start), or the SLA deadline hasn't
  arrived yet this cycle.
- `NOT_EXPECTED` — no cadence firing found (a brand-new registry row, or
  an unrecognized cadence value).

**Hit Rate** is the rolling completion rate over the daily historical
probe's lookback window (`history.json`; 12 cycles for the weekly
cadence, 30 for weekday/EOD cadences) — `—` for continuous-cadence rows,
which the historical probe doesn't cover, and for a not-yet-covered
registry row.

**Reconciliation (config-I2861):** this table renders the freshness
monitor + its historical sweep; it does not add a fourth monitoring
plane. `alpha-engine-pipeline-watchdog` (daily 14:00 UTC start-state
check) and the Saturday/CI Watch resilience agents are visible on
[Fleet Status](/fleet-status), not duplicated here.
        """
    )
