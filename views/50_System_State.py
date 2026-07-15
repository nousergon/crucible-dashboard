"""
System State — Alpha Engine Library (private console)

Browsable surface for the ``alpha-engine-config`` "what is true RIGHT NOW"
doc — ``SYSTEM_STATE.md`` (thin index) + the per-repo/axis
``system_state/*.md`` files it points at (durable cross-repo invariants,
cross-repo arcs mid-flight, and one file per fleet repo). Rendered as-is
via ``st.markdown`` — this page does not parse or curate the content, it
is a read-only window onto the source-of-truth files that live in the
gitignored-from-here ``alpha-engine-config`` private repo (co-located on
this EC2 instance by ``infrastructure/boot-pull.sh``).

Part of the Library surface (config#2588). Companion tabs: Architecture
(``51_Architecture_Doc.py``), Experiments Log (``52_Experiments_Log.py``),
Generated Status (``53_Status_Generated.py``). Registries
(``ARTIFACT_REGISTRY.yaml`` / ``OBSERVATION_REGISTRY.yaml``) are NOT
duplicated here — they already have dedicated deep-dive pages under
Observability (Artifact Freshness / Active Observations); this page just
links to them.

**Loader:** ``loaders/system_docs_loader.py``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from loaders.system_docs_loader import (
    SYSTEM_STATE_FILES,
    load_system_state_file,
    load_system_state_index,
)

st.title("System State")
st.caption(
    "`alpha-engine-config/private-docs/SYSTEM_STATE.md` + `system_state/*.md` — "
    "durable invariants, cross-repo arcs mid-flight, and per-repo known-state "
    "notes. Read-only mirror of the source files; edit them in the "
    "alpha-engine-config repo, not here."
)

st.info(
    "Registries (`ARTIFACT_REGISTRY.yaml` / `OBSERVATION_REGISTRY.yaml`) live "
    "on their own deep-dive pages, not here — see "
    "[Artifact Freshness](/host_observability?tab=Artifact+Freshness) and "
    "[Active Observations](/host_observability?tab=Active+Observations).",
    icon="🔗",
)

index_doc = load_system_state_index()
if index_doc is None:
    st.warning(
        "SYSTEM_STATE.md not reachable from this instance — expected the "
        "alpha-engine-config repo to be co-located via boot-pull.sh (EC2) or "
        "as a `~/Development` / repo-sibling checkout (local dev).",
        icon="⚠️",
    )
else:
    with st.expander("SYSTEM_STATE.md (index)", expanded=True):
        st.caption(f"Source: `{index_doc['source_path']}`")
        st.markdown(index_doc["content"])

st.divider()
st.subheader("Per-repo / per-axis detail")

label = st.selectbox("Axis", list(SYSTEM_STATE_FILES.keys()), key="system_state_axis")
filename = SYSTEM_STATE_FILES[label]
axis_doc = load_system_state_file(filename)

if axis_doc is None:
    st.warning(
        f"`system_state/{filename}` not reachable from this instance.",
        icon="⚠️",
    )
else:
    st.caption(f"Source: `{axis_doc['source_path']}`")
    st.markdown(axis_doc["content"])
