"""Shared card renderer + one-shot write-action guard for the two
human-gated queue pages (config#3060): the Decision Queue (views/49,
gate:decision — judgment calls) and the Action Queue (views/50,
gate:operator/gate:device — Brian's-hands items). Both render one card per
gated issue/PR and post a ruling through the same
``loaders.decision_queue_loader`` write path; only the framing differs —
Decision Queue asks "which option", Action Queue asks "done yet".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

from loaders.decision_queue_loader import defer_issue, kill_issue, post_ruling, send_to_session

_GATE_BADGE = {
    "gate:operator": "🔧 operator action",
    "gate:decision": "⚖️ decision",
    "gate:device": "🔬 device check",
}


def act_once(state_key: str, item_key: str, outcome: str, fn, *args, **kwargs) -> None:
    """Run a write action exactly once per item per session; fail LOUD.

    ``state_key`` namespaces the done-set per page (``dq_done``/``aq_done``)
    so ruling on an item in one queue never masks its twin's session state.
    """
    done = st.session_state.setdefault(state_key, {})
    if item_key in done:
        return
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # surface the API error on the page, never silent
        st.error(f"{item_key}: write failed — {exc}")
        return
    done[item_key] = outcome
    st.toast(f"{item_key}: {outcome}")


def render_card(item: dict, *, state_key: str, index: int | None = None,
                is_action: bool = False) -> None:
    """Render one gate item card with its ruling controls.

    ``index`` (1-based) prefixes the card as ``#N`` — the Action Queue's
    numbered-list presentation Brian asked for (2026-07-20 ruling); the
    Decision Queue passes ``None`` and stays unnumbered, oldest-first.
    ``is_action`` swaps the unframed-item button from "Post ruling" to
    "✅ Mark done" — an operator action's default resolution is "did it",
    not "here's my judgment call".
    """
    key = item["key"]
    with st.container(border=True):
        left, right = st.columns([5, 1])
        kind_badge = "🔀 PR" if item["is_pr"] else "📋 issue"
        prefix = f"**#{index}** " if index is not None else ""
        left.markdown(
            f"{prefix}**[{key}]({item['url']})** — {item['title']}  \n"
            f"{kind_badge} · {_GATE_BADGE.get(item['gate'], item['gate'])} · "
            f"open **{item['age_days']}d**"
        )
        if item["summary"]:
            st.markdown(f"📋 {item['summary']}")
        if item["ask"]:
            st.markdown(f"**Ask:** {item['ask']}")
            for letter, text in item["options"]:
                st.markdown(f"- **{letter})** {text}")
            if item["sota"] or item["delta"]:
                lines = []
                if item["sota"]:
                    lines.append(f"🏛 **SOTA:** {item['sota']}")
                if item["delta"]:
                    lines.append(f"↔ **Delta:** {item['delta']}")
                st.caption("  \n".join(lines))
        else:
            right.markdown("🏷️ `needs framing`")
            with st.expander("Newest gate comment / body excerpt"):
                st.markdown(item["excerpt"] or "_no comment found_")

        rec = item["recommended"]
        letters = [l for l, _ in item["options"]]

        if letters:
            # One-tap per lettered option — every option posts its ruling
            # directly on click (config#736 fix — a reveal-toggle button
            # silently re-armed a form defaulting back to the recommended
            # option, so a repeated non-recommended click never posted).
            opt_cols = st.columns(len(letters))
            for i, (letter, text) in enumerate(item["options"]):
                opt_prefix = "✅ " if letter == rec else ""
                suffix = " (recommended)" if letter == rec else ""
                label = f"{opt_prefix}{letter}) {text[:50]}{suffix}"
                if opt_cols[i].button(label, key=f"opt-{key}-{letter}"):
                    act_once(state_key, key, f"ruled {letter}", post_ruling,
                              item["repo"], item["number"], f"Option {letter}",
                              is_pr=item["is_pr"])
                    st.rerun()

        action_cols = st.columns(3)
        if action_cols[0].button("⏸ Defer 2w", key=f"def-{key}"):
            # UTC date — must match the loader's snoozed-until comparison.
            act_once(state_key, key, "deferred 2w", defer_issue, item["repo"], item["number"],
                      (datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat(),
                      item["body"])
            st.rerun()
        if action_cols[1].button("💬 Session", key=f"ses-{key}", help="Needs discussion — park for /backlog-triage"):
            act_once(state_key, key, "sent to session", send_to_session, item["repo"], item["number"])
            st.rerun()
        if action_cols[2].button("🗑 Kill", key=f"kill-{key}"):
            act_once(state_key, key, "killed", kill_issue, item["repo"], item["number"])
            st.rerun()

        # Free-form ruling — only for unframed items (no lettered options
        # exist to render as one-tap buttons above).
        if not item["ask"]:
            with st.form(key=f"form-{key}", border=False):
                default_label = "✅ Mark done" if is_action else "Post ruling → de-gate"
                placeholder = "What did you do? (optional)" if is_action else "Ruling / rationale (one line)"
                detail = st.text_input(placeholder, key=f"txt-{key}")
                if st.form_submit_button(default_label):
                    outcome = "done" if is_action else "ruled free-form"
                    ruling_label = "Done" if is_action else "Ruling"
                    act_once(state_key, key, outcome, post_ruling,
                              item["repo"], item["number"], ruling_label, detail,
                              is_pr=item["is_pr"])
                    st.rerun()
