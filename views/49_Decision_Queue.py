"""
Decision Queue — Alpha Engine (private console)

The PRIMARY review surface for the human-gated backlog pool (config#1926).
Every open issue carrying ``gate:operator`` / ``gate:decision`` across the
four backlog repos renders as one card, oldest first, with the structured
``**Ask:**`` block (config#1923) and one-tap ruling buttons. A ruling posts
the operator-decision comment and strips the gate label — the next tier
groom (3x/day) executes it. The operator's tap is the authorization; the
groom fleet is the hands.

Write scope: the GitHub issue tracker ONLY (ARCHITECTURE.md carve-out).
This page never writes S3 config, SSM params, or any trading state.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.decision_queue_loader import (  # noqa: E402
    clear_queue_cache,
    defer_issue,
    github_token,
    kill_issue,
    load_decision_queue,
    post_ruling,
    send_to_session,
)

_GATE_BADGE = {
    "gate:operator": "🔧 operator action",
    "gate:decision": "⚖️ decision",
}

st.title("🗳 Decision Queue")
st.caption(
    "Human-gated backlog items, oldest first — one tap posts your ruling to "
    "the issue and de-gates it; the next tier groom executes. "
    "(config#1923/#1926; write scope = issue tracker only)"
)

if github_token() is None:
    st.error(
        "No GitHub token available — SSM `/alpha-engine/groom/github_pat` is "
        "unreadable from this box (dashboard-role needs `ssm:GetParameter` on "
        "it — `iam/alpha-engine-dashboard-role/alpha-engine-dashboard-groom-pat-read.json`) "
        "and no env fallback is set. The queue cannot load."
    )
    st.stop()

# ── one-shot action guard: survive Streamlit reruns / double-clicks ─────────
if "dq_done" not in st.session_state:
    st.session_state.dq_done = {}  # key -> outcome string


def _act(item_key: str, outcome: str, fn, *args) -> None:
    """Run a write action exactly once per item per session; fail LOUD."""
    if item_key in st.session_state.dq_done:
        return
    try:
        fn(*args)
    except Exception as exc:  # surface the API error on the page, never silent
        st.error(f"{item_key}: write failed — {exc}")
        return
    st.session_state.dq_done[item_key] = outcome
    st.toast(f"{item_key}: {outcome}")


try:
    queue = load_decision_queue()
except Exception as exc:
    st.error(f"Decision queue load failed: {exc}")
    st.stop()

pending = [q for q in queue if q["key"] not in st.session_state.dq_done]

c1, c2, c3 = st.columns(3)
c1.metric("Pending decisions", len(pending))
c2.metric("Oldest", f"{pending[0]['age_days']}d" if pending else "—")
c3.metric("Ruled this session", len(st.session_state.dq_done))

if st.button("🔄 Refresh queue"):
    clear_queue_cache()
    st.rerun()

if not pending:
    st.success("Queue clear — nothing is gated on you. 🎉")
    st.stop()

for item in pending:
    key = item["key"]
    with st.container(border=True):
        left, right = st.columns([5, 1])
        left.markdown(
            f"**[{key}]({item['url']})** — {item['title']}  \n"
            f"{_GATE_BADGE.get(item['gate'], item['gate'])} · open **{item['age_days']}d**"
        )
        if item["ask"]:
            st.markdown(f"**Ask:** {item['ask']}")
            for letter, text in item["options"]:
                st.markdown(f"- **{letter})** {text}")
        else:
            right.markdown("🏷️ `needs framing`")
            with st.expander("Newest gate comment / body excerpt"):
                st.markdown(item["excerpt"] or "_no comment found_")

        cols = st.columns(5)
        rec = item["recommended"]
        letters = [l for l, _ in item["options"]]
        # Approve = the recommended option when framed, else a free ruling below.
        if rec:
            if cols[0].button(f"✅ Approve ({rec})", key=f"app-{key}"):
                _act(key, f"approved {rec}", post_ruling, item["repo"], item["number"], f"Option {rec}")
                st.rerun()
            alt = [l for l in letters if l != rec]
            if alt and cols[1].button(f"↔️ {'/'.join(alt)}", key=f"alt-{key}", help="Pick a non-recommended option below"):
                st.session_state[f"show-alt-{key}"] = True
        if cols[2].button("⏸ Defer 2w", key=f"def-{key}"):
            _act(key, "deferred 2w", defer_issue, item["repo"], item["number"],
                 (date.today() + timedelta(days=14)).isoformat())
            st.rerun()
        if cols[3].button("💬 Session", key=f"ses-{key}", help="Needs discussion — park for /backlog-triage"):
            _act(key, "sent to session", send_to_session, item["repo"], item["number"])
            st.rerun()
        if cols[4].button("🗑 Kill", key=f"kill-{key}"):
            _act(key, "killed", kill_issue, item["repo"], item["number"])
            st.rerun()

        # Non-recommended option pick, or free-form ruling for unframed items.
        if st.session_state.get(f"show-alt-{key}") or not item["ask"]:
            with st.form(key=f"form-{key}", border=False):
                choice = st.selectbox(
                    "Option", letters or ["free-form"], key=f"opt-{key}",
                ) if letters else "ruling"
                detail = st.text_input("Ruling / rationale (one line)", key=f"txt-{key}")
                if st.form_submit_button("Post ruling → de-gate"):
                    label = f"Option {choice}" if letters else "Ruling"
                    _act(key, f"ruled {choice}", post_ruling,
                         item["repo"], item["number"], label, detail)
                    st.rerun()
