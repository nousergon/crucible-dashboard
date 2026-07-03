"""
Morning Signal — Content Schedule (private console)

Calendar editor for the morning-signal podcast's per-date schedule manifest
(``s3://morning-signal-podcast/schedule/schedule.json``, contract schema v1 —
see ``loaders/morning_signal_schedule.py``). Three entry modes, consumed by
the generator (morning-signal ``schedule_override.py``, PR #92):

- **override** 🎯 — the episode replaces regular programming with a deep dive
  on the scheduled topic (live-researched via the coverage guard).
- **extend** ➕ — one extra segment on top of the regular lineup.
- **skip** 🚫 — no episode that day (travel/vacation); the generate guard and
  the freshness watchdog both honor it.

"Aired" ✅ badges come from generator-written markers under
``schedule/applied/`` — ground truth that an entry was actually applied, not
a self-report. Fail-soft everywhere: an unreadable manifest degrades the page
to a banner (and the generator independently degrades to regular
programming); a save conflict (concurrent edit) reloads instead of
clobbering (S3 conditional writes).
"""

from __future__ import annotations

import calendar as _calmod  # noqa: F401 — avoid shadowing by the component import
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.morning_signal_schedule import (  # noqa: E402
    VALID_EDITIONS,
    delete_entry,
    load_applied_markers,
    load_schedule,
    upsert_entry,
)

_PACIFIC = ZoneInfo("America/Los_Angeles")

_MODE_META = {
    "override": {"icon": "🎯", "color": "#7c3aed", "label": "Deep-dive override"},
    "extend": {"icon": "➕", "color": "#2563eb", "label": "Extra segment"},
    "skip": {"icon": "🚫", "color": "#6b7280", "label": "Skip day (no episode)"},
}


def _today_pacific() -> date:
    """The generator stamps run dates on the Pacific clock — match it."""
    return datetime.now(_PACIFIC).date()


def _clicked_date(state: object) -> str | None:
    """Extract a YYYY-MM-DD from a streamlit-calendar callback payload
    (dateClick or eventClick), tolerating shape drift."""
    if not isinstance(state, dict):
        return None
    date_click = state.get("dateClick") or {}
    raw = date_click.get("date") or (
        (state.get("eventClick") or {}).get("event") or {}
    ).get("start")
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    return None


def _entry_events(entries: dict, applied: dict) -> list[dict]:
    events = []
    for date_str, entry in sorted(entries.items()):
        meta = _MODE_META.get(entry.get("mode"), _MODE_META["extend"])
        aired = any(
            key.startswith(f"{date_str}-") for key in applied
        )
        title = entry.get("topic") or entry.get("guidance") or entry.get("mode", "")
        prefix = "✅ " if aired else ""
        events.append(
            {
                "id": date_str,
                "title": f"{prefix}{meta['icon']} {title}"[:80],
                "start": date_str,
                "allDay": True,
                "backgroundColor": meta["color"],
                "borderColor": meta["color"],
            }
        )
    return events


st.title("🗓 Morning Signal — Content Schedule")
st.caption(
    "Schedule deep-dive overrides, extra segments, or skip days for the "
    "podcast. The generator reads this manifest at 05:00 PT; ✅ marks entries "
    "it actually applied. Weekend deep dives ride the weekend edition (AM)."
)

manifest, etag, error = load_schedule()
if error:
    st.error(
        f"Schedule unreadable — the generator will run regular programming "
        f"until this is fixed: {error}"
    )
applied_markers = load_applied_markers()
entries: dict = manifest.get("entries", {})

# ── calendar ─────────────────────────────────────────────────────────────────

try:
    from streamlit_calendar import calendar
except ImportError:
    st.error(
        "The `streamlit-calendar` component is not installed in this venv — "
        "run `pip install -r requirements.txt` on the box and restart the "
        "dashboard service."
    )
    st.stop()

st.session_state.setdefault("ms_cal_nonce", 0)
st.session_state.setdefault("ms_sel_date", None)
st.session_state.setdefault("ms_cal_last_payload", None)

cal_state = calendar(
    events=_entry_events(entries, applied_markers),
    options={
        "initialView": "dayGridMonth",
        "headerToolbar": {"left": "prev,next today", "center": "title", "right": ""},
        "height": 650,
        "selectable": True,
    },
    callbacks=["dateClick", "eventClick"],
    # The component doesn't re-render events under a fixed key — embed a
    # nonce bumped on every save/delete so edits show up immediately.
    key=f"ms_cal_{st.session_state['ms_cal_nonce']}",
)

# The component replays its last callback payload on every rerun — only act
# on a payload we haven't processed yet.
if cal_state and cal_state != st.session_state["ms_cal_last_payload"]:
    st.session_state["ms_cal_last_payload"] = cal_state
    clicked = _clicked_date(cal_state)
    if clicked:
        st.session_state["ms_sel_date"] = clicked

# ── entry editor ─────────────────────────────────────────────────────────────

st.divider()
left, right = st.columns([1, 2])
with left:
    fallback = st.date_input(
        "Date",
        value=(
            date.fromisoformat(st.session_state["ms_sel_date"])
            if st.session_state["ms_sel_date"]
            else _today_pacific()
        ),
        help="Click a calendar day above, or pick here.",
    )
sel_date = fallback.isoformat()
existing = entries.get(sel_date)
is_past = fallback < _today_pacific()

with right:
    if existing:
        meta = _MODE_META.get(existing.get("mode"), _MODE_META["extend"])
        aired_keys = sorted(
            k for k in applied_markers if k.startswith(f"{sel_date}-")
        )
        badge = f" — ✅ applied ({', '.join(aired_keys)})" if aired_keys else ""
        st.info(
            f"{meta['icon']} **{sel_date}** has a scheduled "
            f"**{existing.get('mode')}**{badge}"
        )
    else:
        st.caption(f"No entry for {sel_date} — regular programming.")

if is_past:
    st.caption("Past date — read-only.")
    if existing:
        st.json(existing)
else:
    mode = st.selectbox(
        "Mode",
        options=list(_MODE_META),
        index=list(_MODE_META).index(existing["mode"]) if existing else 0,
        format_func=lambda m: f"{_MODE_META[m]['icon']} {_MODE_META[m]['label']}",
    )

    with st.form("ms_schedule_editor"):
        if mode != "skip":
            topic = st.text_input(
                "Topic (required)", value=(existing or {}).get("topic", "")
            )
        else:
            topic = ""
        guidance = st.text_area(
            "Guidance" + (" / reason" if mode == "skip" else ""),
            value=(existing or {}).get("guidance", ""),
            help=(
                "Freeform steer for the episode prompt (override/extend) or "
                "the reason for the skip."
            ),
        )
        editions = st.multiselect(
            "Editions",
            options=list(VALID_EDITIONS),
            default=(existing or {}).get(
                "editions", list(VALID_EDITIONS) if mode == "skip" else ["am"]
            ),
            help=(
                "Which runs this entry applies to. Weekend runs are AM. "
                "For skip, select both to suppress the whole day."
            ),
        )
        if mode != "skip":
            min_searches = st.number_input(
                "Min dedicated web searches",
                min_value=1,
                max_value=10,
                value=int(
                    (existing or {}).get(
                        "min_searches", 3 if mode == "override" else 1
                    )
                ),
                help=(
                    "Coverage-guard floor: the episode must run at least this "
                    "many searches matching the topic keywords or a forced "
                    "recovery pass fires."
                ),
            )
            keywords_raw = st.text_input(
                "Guard keywords (comma-separated; blank = derived from topic)",
                value=", ".join((existing or {}).get("keywords", [])),
            )
        submitted = st.form_submit_button("💾 Save entry", type="primary")

    if submitted:
        entry: dict = {"mode": mode}
        if mode != "skip":
            if not topic.strip():
                st.error("Topic is required for override/extend.")
                st.stop()
            entry["topic"] = topic.strip()
            entry["min_searches"] = int(min_searches)
            keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
            if keywords:
                entry["keywords"] = keywords
        if guidance.strip():
            entry["guidance"] = guidance.strip()
        if not editions:
            st.error("Select at least one edition.")
            st.stop()
        entry["editions"] = editions
        ok, msg = upsert_entry(sel_date, entry)
        if ok:
            st.cache_data.clear()
            st.session_state["ms_cal_nonce"] += 1
            st.success(f"Saved {mode} for {sel_date}.")
            st.rerun()
        elif msg == "conflict":
            st.cache_data.clear()
            st.warning(
                "Schedule changed underneath this tab — reloaded; re-apply "
                "your edit."
            )
            st.rerun()
        else:
            st.error(f"Save failed — {msg}")

    if existing and st.button(f"🗑 Delete entry for {sel_date}"):
        ok, msg = delete_entry(sel_date)
        if ok:
            st.cache_data.clear()
            st.session_state["ms_cal_nonce"] += 1
            st.success(f"Deleted entry for {sel_date}.")
            st.rerun()
        elif msg == "conflict":
            st.cache_data.clear()
            st.warning("Schedule changed underneath this tab — reloaded.")
            st.rerun()
        else:
            st.error(f"Delete failed — {msg}")

# ── upcoming entries ─────────────────────────────────────────────────────────

st.divider()
st.subheader("Scheduled entries")
if not entries:
    st.caption("Nothing scheduled — every day is regular programming.")
else:
    import pandas as pd

    rows = []
    for date_str, entry in sorted(entries.items()):
        aired_keys = sorted(
            k for k in applied_markers if k.startswith(f"{date_str}-")
        )
        meta = _MODE_META.get(entry.get("mode"), _MODE_META["extend"])
        rows.append(
            {
                "Date": date_str,
                "Mode": f"{meta['icon']} {entry.get('mode')}",
                "Topic": entry.get("topic") or entry.get("guidance") or "—",
                "Editions": ", ".join(entry.get("editions") or ["am"]),
                "Aired": "✅ " + ", ".join(
                    k.split("-")[-1] for k in aired_keys
                ) if aired_keys else "",
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

st.caption(
    "Contract: schedule/schedule.json (schema v1) in the morning-signal "
    "podcast bucket; consumer = morning_signal.schedule_override (PR #92). "
    "Writes are etag-conditional (concurrent edits reload, never clobber). "
    "IAM: dashboard role is scoped to schedule/* only."
)
