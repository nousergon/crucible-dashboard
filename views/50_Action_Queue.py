"""
Action Queue — Alpha Engine (private console)

The surface for operator-HANDS items (config#3060): every open issue OR PR
carrying ``gate:operator`` or ``gate:device`` — things already resolved in
principle (a ruling was already made) but blocked purely on Brian physically
doing something: an OpenRouter/console setting, a credential rotation, a
billing step, validating a piece of hardware. Split out of the Decision
Queue (views/49) on Brian's 2026-07-20 ruling: "if a decision/ruling leads
to something I have to do specifically, the decision queue is not the place
to do that." This page is the numbered action list — worked through in a
dedicated pass, not interleaved with genuine ambiguous tradeoffs.

Presentation differs from the Decision Queue in two ways: items are numbered
(#1, #2, ...) for reference during a triage pass, and an unframed item's
default resolution button reads "✅ Mark done" rather than "Post ruling" —
the default framing here is "did you do it", not "what's your judgment".

Write scope: the GitHub issue/PR tracker ONLY (ARCHITECTURE.md carve-out,
same as the Decision Queue). This page never writes S3 config, SSM params,
or any trading state.
"""

from __future__ import annotations

import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from components.gate_queue_card import render_card  # noqa: E402
from loaders.decision_queue_loader import (  # noqa: E402
    clear_queue_cache,
    github_token,
    load_action_queue,
)

_STATE_KEY = "aq_done"

st.title("🔧 Action Queue")
st.caption(
    "Operator-hands issues AND PRs (gate:operator/gate:device), numbered, "
    "oldest first — work top to bottom. One tap posts the ruling and "
    "de-gates it; a fully-unblocked draft PR flips ready immediately. "
    "Judgment calls live on the Decision Queue instead. (config#3060; "
    "write scope = issue/PR tracker only)"
)

if github_token() is None:
    st.error(
        "No GitHub token available — App-token mint failed (SSM "
        "`/alpha-engine/groom/github_app_*`, config-I2785), the groom-PAT "
        "fallback at `/alpha-engine/groom/github_pat` is unreadable "
        "(`alpha-engine-dashboard-role` needs `ssm:GetParameter` on both — "
        "the live `alpha-engine-ssm-read` inline policy's `/alpha-engine/*` "
        "grant covers them), and no env fallback is set. The queue cannot load."
    )
    st.stop()

# ── one-shot action guard: survive Streamlit reruns / double-clicks ─────────
if _STATE_KEY not in st.session_state:
    st.session_state[_STATE_KEY] = {}  # key -> outcome string

try:
    data = load_action_queue()
except Exception as exc:
    st.error(f"Action queue load failed: {exc}")
    st.stop()

snoozed = data["snoozed"]
pending = [q for q in data["items"] if q["key"] not in st.session_state[_STATE_KEY]]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending actions", len(pending))
c2.metric("Oldest", f"{pending[0]['age_days']}d" if pending else "—")
c3.metric("Done this session", len(st.session_state[_STATE_KEY]))
c4.metric("Deferred", len(snoozed), help="Hidden until their Re-exam date arrives")

if st.button("🔄 Refresh queue"):
    clear_queue_cache()
    st.rerun()

if snoozed:
    with st.expander(f"⏸ {len(snoozed)} deferred — re-enter the queue on their Re-exam date"):
        for s in snoozed:
            st.markdown(f"- **[{s['key']}]({s['url']})** — {s['title']} · until **{s['until']}**")

if not pending:
    st.success("Action queue clear — nothing needs your hands. 🎉")
    st.stop()

for i, item in enumerate(pending, start=1):
    render_card(item, state_key=_STATE_KEY, index=i, is_action=True)
