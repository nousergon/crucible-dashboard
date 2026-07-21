"""
Decision Queue — Alpha Engine (private console)

The PRIMARY review surface for the human-gated backlog (config#1926): every
open issue OR PR carrying ``gate:decision``/``gate:operator``/``gate:device``
renders as one card, oldest first, with the structured ``**Ask:**`` block
(config#1923) and one-tap ruling buttons. A ruling posts the
operator-decision comment and strips the gate label; for a PR, it ALSO flips
a fully-unblocked draft ready for review immediately (config#2431) rather
than waiting on a groom pass to notice. The next tier groom (3x/day)
executes any remaining work. The operator's tap is the authorization; the
groom fleet is the hands.

config-I3060 (2026-07-20) split ``gate:operator``/``gate:device`` off to a
separate Action Queue page. config-I3239 (2026-07-21, Brian's ruling)
recombined them — the split added a "which page is this on" routing
question with no reliable way to answer it, worse than the interleaving
problem it solved. Operator/device items still render with "Mark done"
framing instead of "Post ruling" (derived per item from its gate, not from
a page), so the distinction the split was trying to preserve survives —
just not as a second page.

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
    ACTION_SHAPED_GATE_LABELS,
    clear_queue_cache,
    github_token,
    load_decision_queue,
)

_STATE_KEY = "dq_done"

st.title("🗳 Decision Queue")
st.caption(
    "Human-gated issues AND PRs (gate:decision/gate:operator/gate:device), "
    "oldest first — one tap posts your ruling (or marks an operator/device "
    "item done) and de-gates it; a fully-unblocked draft PR flips ready "
    "immediately, the next tier groom executes any remaining work. "
    "(config#1923/#1926/#2421/#3245; write scope = issue/PR tracker only)"
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
    render_card(item, state_key=_STATE_KEY,
                is_action=item["gate"] in ACTION_SHAPED_GATE_LABELS)
