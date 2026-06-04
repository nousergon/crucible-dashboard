"""
Active Observations — Alpha Engine (private console)

Operator surface for the observation-registry arc (alpha-engine-config
``OBSERVATION_REGISTRY.yaml`` SoT + ``scripts/validate_observation_registry.py``
PR-time chokepoint, shipped 2026-05-28).

Companion to /Artifact_Freshness (the freshness axis — "does the
artifact land on time?"). This page is the **observation axis** —
"what's currently in observe-mode, what state is each entry in, and
what's the cutover gate to flip from gated-off / gated-on into
load-bearing production reliance?"

Per ``feedback_observe_mode_unconditional_gates_govern_cutover``
(2026-05-28): observe-mode producer code runs unconditionally;
activation flags gate ONLY the consumer-cutover transition. This
page makes the gates visible so a soak that passes its acceptance
criterion isn't silently forgotten.

**SoT:** ``alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml``
**Loader:** ``loaders/observation_registry_loader.py``
**KPI strip:** /System_Health (Section 0.5)
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.observation_registry_loader import (
    load_observation_registry,
    summarize_by_phase,
    summarize_by_state,
)



_STATE_COLOR: dict[str, str] = {
    "always-on": "#1a7f37",   # promoted / unconditional — green
    "gated-on": "#bf8700",    # active soak — amber
    "gated-off": "#57606a",   # blocked on consumer / preconditions — gray
}

_STATE_LABEL: dict[str, str] = {
    "always-on": "✅ always-on",
    "gated-on": "🟡 gated-on (soak)",
    "gated-off": "⏸ gated-off",
}

_PHASE_LABEL: dict[str, str] = {
    "substrate": "🧱 substrate",
    "observe": "👁 observe",
    "cutover": "🔁 cutover",
    "promoted": "✅ promoted",
}

_VERIFICATION_LABEL: dict[str, str] = {
    "verified": "✅",
    "audit-found-needs-curation": "🔍",
}


# ── Page header ────────────────────────────────────────────────────────────

st.title("👁 Active Observations")
st.caption(
    "Declarative SoT for in-flight observe-mode rollouts. Sibling to "
    "[/Artifact_Freshness](/Artifact_Freshness) — that surface tracks "
    "*'does the artifact land?'*; this one tracks "
    "*'is the consumer plumbed to read it yet, and what's the cutover gate?'*. "
    "SoT: `alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml`."
)


registry = load_observation_registry()

if registry is None:
    st.error(
        "OBSERVATION_REGISTRY.yaml not found. Loader looked at "
        "`/home/ec2-user/alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml` "
        "(EC2 console path), `~/Development/alpha-engine-config/...` "
        "(local-dev path), and sibling-directory fallbacks. On EC2 the "
        "file is populated by `boot-pull.sh`; locally clone "
        "`alpha-engine-config` next to this repo."
    )
    st.stop()

observations: list[dict] = registry["observations"]
source_path = registry.get("_source_path", "<unknown>")

st.caption(f"Loaded from `{source_path}` — {len(observations)} observations.")


# ── KPI strip ──────────────────────────────────────────────────────────────

state_counts = summarize_by_state(observations)
phase_counts = summarize_by_phase(observations)

st.subheader("Summary")
_kpi_cols = st.columns(7)
_kpi_cols[0].metric("Total", len(observations))
_kpi_cols[1].metric("✅ always-on", state_counts["always-on"])
_kpi_cols[2].metric("🟡 gated-on (soak)", state_counts["gated-on"])
_kpi_cols[3].metric("⏸ gated-off", state_counts["gated-off"])
_kpi_cols[4].metric("🧱 substrate", phase_counts["substrate"])
_kpi_cols[5].metric("🔁 cutover", phase_counts["cutover"])
_kpi_cols[6].metric("✅ promoted", phase_counts["promoted"])


# ── Filters ────────────────────────────────────────────────────────────────

st.divider()
st.subheader("Entries")

filter_cols = st.columns(4)

states_present = sorted({obs.get("state", "") for obs in observations if obs.get("state")})
phases_present = sorted({obs.get("phase", "") for obs in observations if obs.get("phase")})
repos_present = sorted({obs.get("producer_repo", "") for obs in observations if obs.get("producer_repo")})

state_filter = filter_cols[0].multiselect(
    "State",
    states_present,
    default=states_present,
)
phase_filter = filter_cols[1].multiselect(
    "Phase",
    phases_present,
    default=phases_present,
)
repo_filter = filter_cols[2].multiselect(
    "Producer repo",
    repos_present,
    default=repos_present,
)
sort_choice = filter_cols[3].selectbox(
    "Sort",
    [
        "earliest_flip_date (sentinels last)",
        "state then earliest_flip_date",
        "producer_repo then state",
    ],
    index=0,
)


def _filter_entries(entries: list[dict]) -> list[dict]:
    return [
        obs
        for obs in entries
        if obs.get("state") in state_filter
        and obs.get("phase") in phase_filter
        and obs.get("producer_repo") in repo_filter
    ]


def _flip_date_sort_key(obs: dict) -> tuple:
    """Sort ISO dates ascending, then sentinels (`condition-gated`,
    `TBD`) at the end. ISO dates and sentinels mix cleanly because we
    return a 2-tuple: (sentinel-bucket, value)."""
    val = obs.get("earliest_flip_date")
    if isinstance(val, date):
        return (0, val.isoformat())
    if isinstance(val, datetime):
        return (0, val.date().isoformat())
    if isinstance(val, str):
        if val == "condition-gated":
            return (1, "")
        if val == "TBD":
            return (2, "")
        return (0, val)
    return (3, "")


def _sorted(entries: list[dict]) -> list[dict]:
    if sort_choice.startswith("earliest_flip_date"):
        return sorted(entries, key=_flip_date_sort_key)
    if sort_choice.startswith("state then"):
        return sorted(
            entries,
            key=lambda obs: (obs.get("state", ""), _flip_date_sort_key(obs)),
        )
    return sorted(
        entries,
        key=lambda obs: (obs.get("producer_repo", ""), obs.get("state", "")),
    )


filtered = _sorted(_filter_entries(observations))

if not filtered:
    st.info("No entries match the current filters.")
    st.stop()


# ── Table view ─────────────────────────────────────────────────────────────


def _format_flip_date(val) -> str:
    if isinstance(val, (date, datetime)):
        return val.isoformat() if hasattr(val, "isoformat") else str(val)
    return str(val) if val is not None else "—"


def _truncate(s: str | None, limit: int = 120) -> str:
    if not s:
        return ""
    s = " ".join(str(s).split())  # collapse newlines / multi-space
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


table_rows = []
for obs in filtered:
    table_rows.append(
        {
            "observation_id": obs.get("observation_id", ""),
            "state": _STATE_LABEL.get(obs.get("state", ""), obs.get("state", "")),
            "phase": _PHASE_LABEL.get(obs.get("phase", ""), obs.get("phase", "")),
            "producer_repo": obs.get("producer_repo", ""),
            "flag": _truncate(obs.get("flag"), 80),
            "earliest_flip_date": _format_flip_date(obs.get("earliest_flip_date")),
            "cutover_gate": _truncate(obs.get("cutover_gate"), 200),
            "verified": _VERIFICATION_LABEL.get(
                obs.get("verification_status", ""), ""
            ),
            "roadmap_ref": obs.get("roadmap_ref", ""),
        }
    )

df = pd.DataFrame(table_rows)
st.dataframe(df, use_container_width=True, hide_index=True)


# ── Per-entry detail (expanders) ───────────────────────────────────────────

st.divider()
st.subheader("Detail")

for obs in filtered:
    oid = obs.get("observation_id", "<unknown>")
    state = obs.get("state", "")
    phase = obs.get("phase", "")
    label = f"{_STATE_LABEL.get(state, state)}  •  {_PHASE_LABEL.get(phase, phase)}  •  **{oid}**"
    with st.expander(label, expanded=False):
        col_left, col_right = st.columns([2, 1])
        with col_left:
            st.markdown(f"**Producer repo:** `{obs.get('producer_repo', '—')}`")
            st.markdown(
                f"**Producer artifact:** `{obs.get('producer_artifact', '—')}`"
            )
            st.markdown(f"**Flag / gate:** `{obs.get('flag', '—')}`")
            cutover = obs.get("cutover_gate", "—")
            st.markdown(f"**Cutover gate:**\n\n{cutover}")
        with col_right:
            st.markdown(
                f"**Earliest flip:** `{_format_flip_date(obs.get('earliest_flip_date'))}`"
            )
            st.markdown(f"**ROADMAP:** `{obs.get('roadmap_ref', '—')}`")
            st.markdown(
                f"**Verification:** "
                f"{_VERIFICATION_LABEL.get(obs.get('verification_status', ''), '?')} "
                f"`{obs.get('verification_status', '—')}`"
            )
            composes = obs.get("composes_with") or []
            if composes:
                st.markdown("**Composes with:**")
                for ref in composes:
                    st.markdown(f"- `{ref}`")

        evidence = obs.get("evidence") or []
        if evidence:
            st.markdown("**Evidence:**")
            for ev in evidence:
                st.markdown(f"- `{ev}`")


# ── About ──────────────────────────────────────────────────────────────────

st.divider()
with st.expander("About this page", expanded=False):
    st.markdown(
        """
This page reads `alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml`
directly from local disk (no S3 hop, no Lambda dependency — the
registry IS the data).

**State vocabulary:**
- `always-on` — producer runs unconditionally; no flag gate.
- `gated-on` — active soak; flag flipped, observation is collecting.
- `gated-off` — blocked on consumer-side readiness or upstream precondition.

**Phase vocabulary:**
- `substrate` — lib / schema layer landed; no producer yet.
- `observe` — producer wired, consumer not yet plumbed.
- `cutover` — consumer reading the artifact; observability path active alongside legacy.
- `promoted` — consumer-cutover complete; legacy retired.

**Why a separate registry from ARTIFACT_REGISTRY:** freshness is
"does the artifact land on time?"; observation is "is the consumer
plumbed to read it yet?" Sibling concerns, different schemas. See
the YAML header comment for the full rationale.

**Verification status:**
- ✅ `verified` — evidence walked, current code confirms the entry.
- 🔍 `audit-found-needs-curation` — seeded from audit; flips to verified
  as the owning area is touched and wiring is confirmed.
        """
    )
