"""
Merged PRs — fleet merge log with human vs agent attribution.

Reads merged PRs from GitHub Search (``org:nousergon``) and overlays S3
recorded attribution (``ops/pr_merge_attribution/latest.json``). Because
agent self-merges run through the operator PAT, ``mergedBy`` alone cannot
distinguish human from agent — agents MUST call
``alpha-engine-config/scripts/record_agent_merge.py`` at self-merge time
(or add the ``agent-merged`` label before merging).
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.pr_merge_loader import load_merged_prs  # noqa: E402

_SOURCE_LABEL = {
    "human": "🧑 Human",
    "agent": "🤖 Agent",
    "dependabot": "🤖 Dependabot",
    "bot": "⚙️ Bot",
}

st.title("🔀 Merged PRs")
st.caption(
    "Recent merges across the nousergon fleet — link per PR plus human vs "
    "agent attribution. Recorded overrides (S3) beat GitHub defaults."
)

days = st.slider("Look back (days)", min_value=7, max_value=30, value=14, step=1)
source_filter = st.multiselect(
    "Filter by merge source",
    options=list(_SOURCE_LABEL.keys()),
    default=list(_SOURCE_LABEL.keys()),
    format_func=lambda k: _SOURCE_LABEL[k],
)

try:
    rows, total_count = load_merged_prs(days=days)
except Exception as exc:
    st.error(f"Could not load merged PRs: {exc}")
    st.info(
        "On the console box, ``FLOW_DOCTOR_GITHUB_TOKEN`` is hydrated from SSM "
        "at boot. Locally, run ``gh auth login`` or export ``GH_TOKEN``."
    )
    st.stop()

if not rows:
    st.info("No merged PRs in this window.")
    st.stop()

filtered = [r for r in rows if r.get("merge_source") in source_filter]

# Summary strip
counts: dict[str, int] = {}
for r in rows:
    src = r.get("merge_source", "human")
    counts[src] = counts.get(src, 0) + 1

cols = st.columns(len(_SOURCE_LABEL))
for col, (key, label) in zip(cols, _SOURCE_LABEL.items()):
    with col:
        st.metric(label, counts.get(key, 0))

if total_count and total_count > len(rows):
    st.caption(
        f"Showing {len(rows)} of {total_count} merges in window "
        f"(GitHub Search caps at 100 — narrow the date range if needed)."
    )

display_rows = []
for r in filtered:
    src = r.get("merge_source", "human")
    conf = r.get("confidence", "")
    display_rows.append({
        "Merged (UTC)": r.get("merged_at", ""),
        "Repo": r.get("repo", "").replace("nousergon/", ""),
        "PR": r.get("pr", ""),
        "Title": r.get("title", ""),
        "Merged by": r.get("merged_by") or r.get("author") or "—",
        "Source": _SOURCE_LABEL.get(src, src),
        "Confidence": conf,
        "Link": r.get("link") or r.get("url") or "",
    })

st.dataframe(
    pd.DataFrame(display_rows),
    use_container_width=True,
    hide_index=True,
    column_config={
        "Link": st.column_config.LinkColumn("Link", display_text="GitHub ↗"),
        "Title": st.column_config.TextColumn("Title", width="large"),
    },
)

st.divider()
st.markdown(
    "**Attribution discipline**  \n"
    "- **Recorded** — S3 entry written by ``record_agent_merge.py`` at merge time "
    "(authoritative).  \n"
    "- **labeled** — PR carries the ``agent-merged`` label.  \n"
    "- **default** — human unless S3/label says agent (groom-style ``[P2/high]`` "
    "titles are NOT inferred as agent)."
)
