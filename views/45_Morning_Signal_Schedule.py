"""
Morning Signal — Content Schedule (private console)

Calendar editor for the morning-signal podcast's per-date schedule manifest
(``s3://morning-signal-podcast/schedule/schedule.json``, contract schema v1 —
see ``loaders/morning_signal_schedule.py``). Click any day on the calendar
(the FullCalendar wrapper emits single-click dateClick/eventClick, so a
double-click works too) to open a modal editor with four choices, defaulting
to regular programming:

- 📻 **regular** — no entry (saving this removes an existing entry)
- 🎯 **override** — the episode replaces regular programming with a deep dive
  on the scheduled topic (live-researched via the coverage guard)
- ➕ **extend** — one extra segment on top of the regular lineup
- 🚫 **skip** — no episode that day (travel/vacation); the generate guard and
  the freshness watchdog both honor it

Consumed by the generator (morning-signal ``schedule_override.py``, #92).
"Aired" ✅ badges come from generator-written markers under
``schedule/applied/`` — ground truth that an entry was actually applied, not
a self-report. Fail-soft everywhere: an unreadable manifest degrades the page
to a banner (and the generator independently degrades to regular
programming); a save conflict (concurrent edit) reloads instead of
clobbering (S3 conditional writes).

Component quirks (encoded below, keep them): streamlit-calendar does not
re-render events under a fixed ``key`` AND replays its last callback payload
on every rerun — both are handled by remounting the calendar (nonce-embedded
key) immediately after each processed click, with ``initialDate`` pinned to
the clicked date so the remount stays on the same month.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.morning_signal_schedule import (  # noqa: E402
    VALID_EDITIONS,
    delete_entry,
    load_applied_markers,
    load_llm_decisions,
    load_schedule,
    upsert_entry,
)

_PACIFIC = ZoneInfo("America/Los_Angeles")

_MODE_META = {
    "regular": {
        "icon": "📻",
        "color": "",
        "label": "Regular programming (no entry)",
    },
    "override": {
        "icon": "🎯",
        "color": "#7c3aed",
        "label": "Deep-dive override — replaces the episode",
    },
    "extend": {
        "icon": "➕",
        "color": "#2563eb",
        "label": "Extra segment — on top of the regular lineup",
    },
    "skip": {
        "icon": "🚫",
        "color": "#6b7280",
        "label": "Skip day — no episode",
    },
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


# Short display names for the models config#1659's Kimi-primary/Anthropic-
# fallback cascade can report (morning-signal#106) — unrecognized model ids
# fall back to showing the raw string, so a new candidate never goes blank.
_MODEL_SHORT_NAMES = {
    "moonshotai/kimi-k2.6": "Kimi K2.6",
    "xiaomi/mimo-v2.5-pro": "MiMo V2.5",
    "claude-haiku-4-5": "Haiku",
    "claude-sonnet-4-6": "Sonnet",
}


def _model_short_name(model: str | None) -> str:
    if not model:
        return "?"
    return _MODEL_SHORT_NAMES.get(model, model)


def _llm_badge_text(record: dict) -> str:
    """'🤖 Kimi K2.6' normally, '🤖 Kimi K2.6→Haiku' when the fallback fired
    (config#1659) — the fallback arrow is the signal worth a glance."""
    used = _model_short_name(record.get("used_model"))
    if record.get("fell_back"):
        primary = _model_short_name(record.get("primary_model"))
        return f"🤖 {primary}→{used}"
    return f"🤖 {used}"


def _llm_events(decisions: dict) -> list[dict]:
    """One compact calendar event per date showing which model aired that
    day's episode — independent of (and additive to) the scheduled-entry
    events above, since most days have NO schedule entry (regular
    programming) but DO have an llm_decision record. Prefers the 'am'
    edition (the only one the cipher813 deployment actually runs) when a
    date has more than one.
    """
    by_date: dict[str, dict] = {}
    for key, record in decisions.items():
        date_str = record.get("date") or key.rsplit("-", 1)[0]
        if date_str not in by_date or record.get("edition") == "am":
            by_date[date_str] = record

    events = []
    for date_str, record in by_date.items():
        fell_back = bool(record.get("fell_back"))
        color = "#dc2626" if fell_back else "#6b7280"  # red if fallback fired
        events.append(
            {
                "id": f"llm-{date_str}",
                "title": _llm_badge_text(record)[:40],
                "start": date_str,
                "allDay": True,
                "display": "list-item",  # compact dot+text, distinct from the block-style schedule events
                "backgroundColor": color,
                "borderColor": color,
                "textColor": "#ffffff",
            }
        )
    return events


def _entry_events(entries: dict, applied: dict) -> list[dict]:
    events = []
    for date_str, entry in sorted(entries.items()):
        meta = _MODE_META.get(entry.get("mode"), _MODE_META["extend"])
        aired = any(key.startswith(f"{date_str}-") for key in applied)
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


def _finish(flash: tuple[str, str]) -> None:
    """Store a post-rerun toast, refresh data + calendar, close the dialog."""
    st.session_state["ms_flash"] = flash
    st.cache_data.clear()
    st.session_state["ms_cal_nonce"] += 1
    st.rerun()


@st.dialog("📅 Edit day")
def _edit_day_dialog(date_str: str, entry: dict | None, aired_keys: list[str]) -> None:
    """Modal editor for one date. ``entry`` is the existing manifest entry
    (None = regular programming today). Saving 'regular' over an existing
    entry deletes it — regular programming is the default state, not a mode
    stored in the manifest."""
    day = date.fromisoformat(date_str)
    st.subheader(f"{day.strftime('%A, %B %-d, %Y')}")
    if aired_keys:
        st.caption(
            "✅ applied by the generator: "
            + ", ".join(k.split("-")[-1] for k in aired_keys)
        )

    if day < _today_pacific():
        st.caption("Past date — read-only.")
        st.json(entry if entry else {"programming": "regular"})
        return

    current_mode = (entry or {}).get("mode", "regular")
    if current_mode not in _MODE_META:
        current_mode = "regular"
    modes = list(_MODE_META)
    mode = st.radio(
        "Programming",
        modes,
        index=modes.index(current_mode),
        format_func=lambda m: f"{_MODE_META[m]['icon']} {_MODE_META[m]['label']}",
        key=f"dlg_mode_{date_str}",
    )

    new_entry: dict | None = None
    valid = True
    if mode in ("override", "extend"):
        topic = st.text_input(
            "Topic (required)",
            value=(entry or {}).get("topic", ""),
            key=f"dlg_topic_{date_str}",
        )
        guidance = st.text_area(
            "Guidance",
            value=(entry or {}).get("guidance", ""),
            help="Freeform steer for the episode prompt.",
            key=f"dlg_guidance_{date_str}",
        )
        with st.expander("Advanced (editions, search guard)"):
            editions = st.multiselect(
                "Editions",
                options=list(VALID_EDITIONS),
                default=(entry or {}).get("editions", ["am"]),
                help="Which runs this applies to; weekend runs are AM.",
                key=f"dlg_editions_{date_str}",
            )
            min_searches = st.number_input(
                "Min dedicated web searches",
                min_value=1,
                max_value=10,
                value=int(
                    (entry or {}).get(
                        "min_searches", 3 if mode == "override" else 1
                    )
                ),
                key=f"dlg_minsearch_{date_str}",
            )
            keywords_raw = st.text_input(
                "Guard keywords (comma-separated; blank = derived from topic)",
                value=", ".join((entry or {}).get("keywords", [])),
                key=f"dlg_keywords_{date_str}",
            )
        if not topic.strip():
            valid = False
            st.caption("⚠️ Topic is required for override/extend.")
        else:
            new_entry = {
                "mode": mode,
                "topic": topic.strip(),
                "editions": editions or ["am"],
                "min_searches": int(min_searches),
            }
            if guidance.strip():
                new_entry["guidance"] = guidance.strip()
            keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
            if keywords:
                new_entry["keywords"] = keywords
    elif mode == "skip":
        reason = st.text_input(
            "Reason (optional)",
            value=(entry or {}).get("guidance", ""),
            key=f"dlg_reason_{date_str}",
        )
        editions = st.multiselect(
            "Editions to skip",
            options=list(VALID_EDITIONS),
            default=(entry or {}).get("editions", list(VALID_EDITIONS)),
            help="Both = no episode at all that day.",
            key=f"dlg_skip_editions_{date_str}",
        )
        if not editions:
            valid = False
            st.caption("⚠️ Select at least one edition to skip.")
        else:
            new_entry = {"mode": "skip", "editions": editions}
            if reason.strip():
                new_entry["guidance"] = reason.strip()
    else:
        if entry:
            st.caption(
                f"Saving removes the scheduled {entry.get('mode')} and "
                f"returns {date_str} to regular programming."
            )
        else:
            st.caption("Regular programming — nothing stored for this day.")

    save_col, cancel_col = st.columns([1, 1])
    with save_col:
        if st.button("💾 Save", type="primary", disabled=not valid,
                     use_container_width=True, key=f"dlg_save_{date_str}"):
            if mode == "regular":
                if entry:
                    ok, msg = delete_entry(date_str)
                    if ok:
                        _finish(("success", f"{date_str} → regular programming."))
                    elif msg == "conflict":
                        _finish(("warning",
                                 "Schedule changed underneath — reloaded; "
                                 "re-apply your edit."))
                    else:
                        st.error(f"Remove failed — {msg}")
                else:
                    st.rerun()  # nothing to do — close
            else:
                ok, msg = upsert_entry(date_str, new_entry)
                if ok:
                    _finish(("success", f"Saved {mode} for {date_str}."))
                elif msg == "conflict":
                    _finish(("warning",
                             "Schedule changed underneath — reloaded; "
                             "re-apply your edit."))
                else:
                    st.error(f"Save failed — {msg}")
    with cancel_col:
        if st.button("Cancel", use_container_width=True,
                     key=f"dlg_cancel_{date_str}"):
            st.rerun()


st.title("🗓 Morning Signal — Content Schedule")
st.caption(
    "Click any day to set its programming: regular (default), a deep-dive "
    "override, an extra segment, or a skip. The generator reads this "
    "manifest at 05:00 PT; ✅ marks entries it actually applied. Weekend "
    "deep dives ride the weekend edition (AM). 🤖 marks which model "
    "generated that day's episode (config#1659) — red means the Anthropic "
    "fallback had to step in."
)

flash = st.session_state.pop("ms_flash", None)
if flash:
    level, msg = flash
    (st.success if level == "success" else st.warning)(msg)

manifest, etag, error = load_schedule()
if error:
    st.error(
        f"Schedule unreadable — the generator will run regular programming "
        f"until this is fixed: {error}"
    )
applied_markers = load_applied_markers()
llm_decisions = load_llm_decisions()
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
st.session_state.setdefault("ms_cal_initial", _today_pacific().isoformat())

cal_state = calendar(
    events=_entry_events(entries, applied_markers) + _llm_events(llm_decisions),
    options={
        "initialView": "dayGridMonth",
        "initialDate": st.session_state["ms_cal_initial"],
        "headerToolbar": {"left": "prev,next today", "center": "title", "right": ""},
        "height": 650,
        "selectable": True,
    },
    callbacks=["dateClick", "eventClick"],
    # Remount after every processed click (nonce key): the component both
    # replays its last callback payload on every rerun AND won't re-render
    # events under a fixed key — a fresh mount clears the stale payload (so
    # dismissing the dialog and re-clicking the SAME day works) and picks up
    # event changes. initialDate keeps the remount on the month in view.
    key=f"ms_cal_{st.session_state['ms_cal_nonce']}",
)

clicked = _clicked_date(cal_state)
if clicked:
    st.session_state["ms_cal_initial"] = clicked
    st.session_state["ms_cal_nonce"] += 1
    _edit_day_dialog(
        clicked,
        entries.get(clicked),
        sorted(k for k in applied_markers if k.startswith(f"{clicked}-")),
    )

# Fallback path (also useful if the component ever misbehaves): pick a date
# and open the same editor dialog.
pick_col, btn_col = st.columns([1, 3])
with pick_col:
    picked = st.date_input("Or pick a date", value=_today_pacific())
with btn_col:
    st.write("")
    st.write("")
    if st.button("✏️ Edit this date"):
        d = picked.isoformat()
        st.session_state["ms_cal_initial"] = d
        _edit_day_dialog(
            d,
            entries.get(d),
            sorted(k for k in applied_markers if k.startswith(f"{d}-")),
        )

# ── scheduled entries ────────────────────────────────────────────────────────

st.divider()
st.subheader("Scheduled entries")
if not entries:
    st.caption("Nothing scheduled — every day is regular programming.")
else:
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

# ── recent model usage (config#1659) ────────────────────────────────────────
# Independent of the schedule table above: most days have NO schedule entry
# (regular programming) but DO have an llm_decision record, so this covers
# the days the "Scheduled entries" table above intentionally skips.

st.divider()
st.subheader("🤖 Recent model usage")
if not llm_decisions:
    st.caption(
        "No LLM decision records yet — populated once morning-signal#107 "
        "is live and an episode has run."
    )
else:
    llm_rows = []
    for key, record in sorted(llm_decisions.items(), reverse=True)[:14]:
        llm_rows.append(
            {
                "Date": record.get("date", key),
                "Edition": record.get("edition", "—"),
                "Primary": _model_short_name(record.get("primary_model")),
                "Used": _model_short_name(record.get("used_model")),
                "Fell back?": "⚠️ yes" if record.get("fell_back") else "no",
            }
        )
    st.dataframe(
        pd.DataFrame(llm_rows),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("Last 14 records, most recent first.")

st.caption(
    "Contract: schedule/schedule.json (schema v1) in the morning-signal "
    "podcast bucket; consumer = morning_signal.schedule_override (#92). "
    "Writes are etag-conditional (concurrent edits reload, never clobber). "
    "Skips set here are honored by the generate guard AND the watchdog; the "
    "config-file skip_dates list is a separate offline mechanism that does "
    "not appear on this calendar. IAM: dashboard role is scoped to "
    "schedule/* only."
)
