"""
Retros — Alpha Engine (private console)

Surfaces retro material mined **deterministically (no LLM)** from the
system-wide changelog corpus. Data source:
``s3://alpha-engine-research/changelog/retro_candidates.json``, emitted daily
by ``alpha-engine-docs/scripts/emit_retro_candidates.py`` (the
``aggregate-changelog.yml`` cron). Two sections:

  - **Ready for retro** — incidents an operator has already written up
    (root cause + a ≥200-char resolution note). Full editorial detail.
    Empty until annotation happens — narrative is authored manually by
    design (``feedback_retros_scaffold_auto_narrative_manual``); this page
    surfaces the factual scaffold, it does not generate prose.
  - **Incidents to review** — every real high/critical incident in the
    window, grouped by ``(subsystem, normalized summary)`` so recurring
    failures collapse to one counted row. These are the candidates an
    operator triages into the "ready" set.

Depends on correct ``event_type`` / ``severity`` upstream — the
alpha-engine-data changelog-incident-mirror classifier (PR #378) is what
keeps SUCCESS/OK notifications out of the incident pool.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import _fetch_s3_json, _research_bucket

RETRO_KEY = "changelog/retro_candidates.json"
# Mirrors the registry SLA (interval 1d + 2d grace): the feed is regenerated
# daily, so anything older than ~3 days means the docs aggregator has stalled.
_STALE_AFTER_DAYS = 3

_SEVERITY_BADGE = {
    "critical": "🟥 critical",
    "high": "🟧 high",
    "medium": "🟨 medium",
    "low": "🟩 low",
    "informational": "⬜ info",
}


@st.cache_data(ttl=900)
def _load_retro_feed() -> dict | None:
    return _fetch_s3_json(_research_bucket(), RETRO_KEY)


def _fmt_git_refs(refs: list[dict]) -> str:
    parts = []
    for r in refs or []:
        repo = r.get("repo", "")
        pr = r.get("pr_number")
        sha = r.get("sha")
        if pr is not None:
            parts.append(f"[{repo}#{pr}](https://github.com/{repo}/pull/{pr})")
        elif sha:
            parts.append(f"[{repo}@{sha[:7]}](https://github.com/{repo}/commit/{sha})")
        elif repo:
            parts.append(repo)
    return " · ".join(parts)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


st.title("Retros")
st.caption(
    "Retro candidates mined **deterministically (no LLM)** from the system-wide "
    "changelog. Narrative write-ups stay manual — this page surfaces the factual "
    "scaffold so you can decide what's worth a retro."
)

data = _load_retro_feed()

if data is None:
    st.info(
        f"No retro feed yet — `{RETRO_KEY}` not found in S3. It is emitted daily by "
        "`alpha-engine-docs` (`aggregate-changelog.yml`) once that workflow's fix "
        "(PR #26) is merged and the next 06:00 UTC cron runs."
    )
    st.stop()

# ── Freshness / window strip ────────────────────────────────────────────────
generated_at = data.get("generated_at", "?")
window_start = data.get("window_start", "?")
window_end = data.get("window_end", "?")
gen_dt = _parse_ts(generated_at)

cols = st.columns(3)
cols[0].metric("Window", f"{data.get('window_days', '?')}d", help=f"{window_start} → {window_end}")
cols[1].metric("Incident groups", data.get("incident_group_count", 0),
               help=f"{data.get('incident_total', 0)} total occurrences, grouped")
cols[2].metric("Ready for retro", data.get("ready_for_retro_count", 0),
               help="Incidents with an operator-authored writeup")

if gen_dt is not None:
    age_days = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 86400.0
    if age_days > _STALE_AFTER_DAYS:
        st.warning(
            f"⚠️ Feed last generated {generated_at} ({age_days:.1f} days ago) — the daily "
            "changelog aggregator may have stalled. Check `aggregate-changelog.yml`."
        )
    else:
        st.caption(f"Feed generated {generated_at}.")

st.divider()

# ── Section 1: Ready for retro ──────────────────────────────────────────────
ready = data.get("ready_for_retro", [])
st.subheader(f"📝 Ready for retro ({len(ready)})")

if not ready:
    st.caption(
        "None yet. An incident becomes _ready_ once an operator records a root cause "
        "and a ≥200-char resolution writeup via `changelog-log`. Until then it sits in "
        "**Incidents to review** below."
    )
else:
    for c in ready:
        sev = _SEVERITY_BADGE.get(c.get("severity", ""), c.get("severity", "?"))
        with st.expander(f"{sev} · `{c.get('subsystem', '—')}` — {c.get('summary', '')}"):
            meta = [f"**root cause:** `{c.get('root_cause_category') or '—'}`"]
            if c.get("resolution_type"):
                meta.append(f"**resolution:** `{c['resolution_type']}`")
            meta.append(f"`{c.get('ts_utc', '?')}`")
            st.markdown(" · ".join(meta))
            refs = _fmt_git_refs(c.get("git_refs"))
            if refs:
                st.markdown(f"**refs:** {refs}")
            notes = c.get("resolution_notes")
            if notes:
                st.markdown("**Resolution notes:**")
                st.markdown(f"> {notes}")

st.divider()

# ── Section 2: Incidents to review (grouped) ────────────────────────────────
groups = data.get("incident_groups", [])
st.subheader(
    f"🚨 Incidents to review ({len(groups)} distinct · {data.get('incident_total', 0)} total)"
)

if not groups:
    st.success("No high/critical incidents in the window. 🎉")
else:
    df = pd.DataFrame(groups)
    df["severity"] = df["severity"].map(lambda s: _SEVERITY_BADGE.get(s, s))
    df["writeup"] = df["has_writeup"].map(lambda b: "✅" if b else "—")
    df = df.rename(columns={
        "severity": "Severity",
        "count": "Count",
        "subsystem": "Subsystem",
        "summary": "Summary",
        "latest_ts": "Latest",
        "writeup": "Writeup",
    })
    display_cols = ["Severity", "Count", "Subsystem", "Summary", "Latest", "Writeup"]
    st.dataframe(
        df[display_cols],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Count": st.column_config.NumberColumn(width="small"),
            "Summary": st.column_config.TextColumn(width="large"),
        },
    )
    st.caption(
        "Recurring failures are grouped by subsystem + normalized summary (the alarm "
        "name is preserved, so distinct alarms stay separate). To promote one to "
        "**Ready for retro**, write it up with `changelog-log`."
    )
