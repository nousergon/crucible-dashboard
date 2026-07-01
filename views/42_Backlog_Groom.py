"""
Backlog Groom — Alpha Engine (private console)

Operator audit surface for the complexity-tier backlog groom (config#1495,
#1512). Every scheduled groom run (2x/day Sonnet mid/low-tier + 1x/day Opus
high-tier) writes a per-run artifact to
``s3://alpha-engine-research/groom/{date}/{run_id_or_hhmmss}.json``
(``groom_driver.py::write_run_artifact``) — this page is its consumer.

The point of this page: answer "did the model actually think about each
issue?" from VERIFIABLE artifacts, never a self-report. Each queued issue's
disposition (closed / pr_opened / commented / untouched) is cross-referenced
against real GitHub state at write time — a PR link, a close reason, or the
actual latest comment — not a claim the agent made about itself. This is the
same ground-truth-over-self-report principle the run-attribution fix
(config#1512) applies to the PR/close counts in the GitHub digest issue.

Complementary to **Saturday SF Watch** (failure-event timeline for the trading
pipelines) — this page is the per-run activity log for the groom pipeline.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import list_groom_run_keys, load_groom_run  # noqa: E402

_DISPOSITION_LABEL: dict[str, str] = {
    "closed": "✅ closed",
    "pr_opened": "🔧 PR opened",
    "commented": "💬 commented",
    "untouched": "⚠️ untouched",
}
_DISPOSITION_COLOR_HEX: dict[str, str] = {
    "closed": "#1a7f37",
    "pr_opened": "#0969da",
    "commented": "#9a6700",
    "untouched": "#cf222e",
}


def _run_label(key: str) -> str:
    """``groom/{date}/{suffix}.json`` -> ``{date} {suffix}`` for the selector."""
    stem = key.removeprefix("groom/").removesuffix(".json")
    return stem.replace("/", " ")


st.title("🧹 Backlog Groom")
st.caption(
    "Per-run audit trail for the complexity-tier backlog groom — every "
    "issue's disposition cross-referenced against real GitHub state, not a "
    "self-report. (config#1495, #1512)"
)

keys = list_groom_run_keys()
if not keys:
    st.info(
        "🛈 No groom run artifacts found yet. Written by `groom_driver.py` "
        "starting with the config#1512 follow-up — older runs (and any run "
        "before that ships) have no artifact here; check the `groom-digest` "
        "GitHub issues on `alpha-engine-config` for their record instead."
    )
    st.stop()

col_sel, col_meta = st.columns([1, 3])
with col_sel:
    selected_key = st.selectbox(
        "Run", keys, index=0, format_func=_run_label,
    )

data = load_groom_run(selected_key)
if data is None:
    st.warning(f"Run artifact `{selected_key}` could not be read.")
    st.stop()

issues = data.get("issues") or []
other_closed = data.get("other_closed") or []
other_prs = data.get("other_prs") or []

n_closed = sum(1 for i in issues if i.get("disposition") == "closed")
n_pr = sum(1 for i in issues if i.get("disposition") == "pr_opened")
n_commented = sum(1 for i in issues if i.get("disposition") == "commented")
n_untouched = sum(1 for i in issues if i.get("disposition") == "untouched")

with col_meta:
    st.caption(
        f"Model: **{data.get('model', '—')}** · Filter: **{data.get('issue_filter', '—')}** · "
        f"Stop reason: {data.get('stop_reason', '—')}"
    )

tiles = st.columns(5)
with tiles[0]:
    st.metric("Queued issues", len(issues))
with tiles[1]:
    st.metric("✅ Closed", n_closed)
with tiles[2]:
    st.metric("🔧 PR opened", n_pr)
with tiles[3]:
    st.metric("💬 Commented", n_commented)
with tiles[4]:
    st.metric("⚠️ Untouched", n_untouched, delta=None if n_untouched == 0 else "check below",
             delta_color="inverse")

if data.get("floor_fail"):
    st.error(
        "⚠️ FAIL-LOUD FLOOR BREACHED — this run stopped with budget+time "
        "remaining but delivered fewer than the minimum work-items. Treated "
        "as a self-taper failure; see the `groom-digest` GitHub issue."
    )
if n_untouched:
    st.warning(
        f"{n_untouched} queued issue(s) got NO action this run (not closed, "
        "no PR, no comment) — coverage is supposed to be mandatory; an "
        "untouched issue here means it hit the re-queue attempt cap without "
        "ever being dispositioned. Worth checking why."
    )

# ── Per-issue disposition table ─────────────────────────────────────────────
st.subheader("Per-issue disposition")

df = pd.DataFrame(issues)
display = pd.DataFrame({
    "Issue": df.apply(lambda r: f"{r['repo'].rsplit('/', 1)[-1]}#{r['number']}", axis=1) if len(df) else [],
    "Priority": df.get("priority"),
    "Title": df.get("title"),
    "Disposition": df.get("disposition", pd.Series(dtype=str)).map(
        lambda d: _DISPOSITION_LABEL.get(d, d or "—")
    ),
    "Detail": df.get("detail"),
})


def _disposition_style(col: pd.Series) -> list[str]:
    return [
        f"background-color: {_DISPOSITION_COLOR_HEX.get(v, '#6e7781')}; color: white"
        for v in df.get("disposition", pd.Series(dtype=str))
    ]


if len(display):
    st.dataframe(
        display.style.apply(_disposition_style, subset=["Disposition"]),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.caption("No issues in this run's queue (e.g. a clean empty-queue shutdown).")

# ── Other activity (config#1512 — transparent, not hidden) ─────────────────
if other_closed or other_prs:
    with st.expander(
        f"Other activity in this run's window — NOT attributed to this run's queue "
        f"({len(other_closed)} closes, {len(other_prs)} PRs)"
    ):
        st.caption(
            "Concurrent/unrelated work (a different schedule, an interactive "
            "session) that happened to land during this run's wall-clock "
            "window. Excluded from the metrics above and from the run's own "
            "floor-breach check (config#1512) — listed here for transparency."
        )
        for c in other_closed:
            st.markdown(f"- CLOSED `{c['repo'].rsplit('/', 1)[-1]}#{c['number']}` — {c.get('title', '')}")
        for p in other_prs:
            draft = " [draft]" if p.get("draft") else ""
            st.markdown(f"- PR `{p['repo'].rsplit('/', 1)[-1]}#{p['number']}`{draft} — {p.get('title', '')}")

# ── Chunk log ────────────────────────────────────────────────────────────────
if data.get("chunk_log"):
    with st.expander("Per-chunk log"):
        for line in data["chunk_log"]:
            st.markdown(line)

with st.expander("Raw run artifact JSON"):
    st.json(data)
