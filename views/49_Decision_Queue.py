"""
Decision Queue — Alpha Engine (private console)

The PRIMARY review surface for judgment-call items (config#1926; narrowed to
``gate:decision`` only, config#3060). Every open issue OR PR whose product/
scope question is still unresolved renders as one card, oldest first, with
the structured ``**Ask:**`` block (config#1923) and one-tap ruling buttons.
A ruling posts the operator-decision comment and strips the gate label; for
a PR, it ALSO flips a fully-unblocked draft ready for review immediately
(config#2431) rather than waiting on a groom pass to notice. The next tier
groom (3x/day) executes any remaining work. The operator's tap is the
authorization; the groom fleet is the hands.

Operator-HANDS items (``gate:operator``/``gate:device`` — "go click a
setting", "go check the hardware") live on the separate Action Queue
(views/50) instead: Brian's 2026-07-20 ruling is that "a decision/ruling
that leads to something I have to do specifically" doesn't belong in a
queue framed around resolving ambiguity — it belongs on a numbered action
list worked during a dedicated pass.

Write scope: the GitHub issue/PR tracker ONLY (ARCHITECTURE.md carve-out).
This page never writes S3 config, SSM params, or any trading state.
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
    load_decision_queue,
)

_STATE_KEY = "dq_done"

st.title("🗳 Decision Queue")
st.caption(
    "Judgment-call issues AND PRs (gate:decision), oldest first — one tap "
    "posts your ruling and de-gates it; a fully-unblocked draft PR flips "
    "ready immediately, the next tier groom executes any remaining work. "
    "Operator-hands items live on the Action Queue instead. "
    "(config#1923/#1926/#2421/#3060; write scope = issue/PR tracker only)"
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
    data = load_decision_queue()
except Exception as exc:
    st.error(f"Decision queue load failed: {exc}")
    st.stop()

snoozed = data["snoozed"]
pending = [q for q in data["items"] if q["key"] not in st.session_state[_STATE_KEY]]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending decisions", len(pending))
c2.metric("Oldest", f"{pending[0]['age_days']}d" if pending else "—")
c3.metric("Ruled this session", len(st.session_state[_STATE_KEY]))
c4.metric("Deferred", len(snoozed), help="Hidden until their Re-exam date arrives")

if st.button("🔄 Refresh queue"):
    clear_queue_cache()
    st.rerun()

if snoozed:
    with st.expander(f"⏸ {len(snoozed)} deferred — re-enter the queue on their Re-exam date"):
        for s in snoozed:
            st.markdown(f"- **[{s['key']}]({s['url']})** — {s['title']} · until **{s['until']}**")

if not pending:
    st.success("Queue clear — nothing is gated on you. 🎉")
    st.stop()

for item in pending:
    render_card(item, state_key=_STATE_KEY)
