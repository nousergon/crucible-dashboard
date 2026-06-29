"""Overview card renderer for the Phase-2 morning brief (config#664 / L4574).

Lives in the shared top-level ``components/`` package — the live pages resolve
``from components.X import ...`` against this package (see
``live/pages/system_pulse.py`` importing ``components.uptime_kpi``). The brief
LOGIC, however, lives under ``live/`` (``live/morning_brief.py``); it is imported
lazily inside ``render_morning_brief_card`` so this module stays import-safe from
any entrypoint (the console doesn't have ``live/`` on its path) and only pulls in
the live brief machinery when actually rendered on the Live Portfolio page.

Renders the brief resolved by ``live.morning_brief.get_or_generate_brief`` at the
top of the Live Portfolio page. Cases:

  * kill switch OFF        → nothing rendered (regulatory disable is silent).
  * window CLOSED          → last brief stamped "as of HH:MM ET" + closed flag.
  * window OPEN, brief set  → the brief, stamped "as of HH:MM ET".
  * no brief yet           → a friendly "preparing" / "unavailable" caption.

Pure presentation — all decision/generation logic lives upstream.
"""

from __future__ import annotations

import streamlit as st


def render_morning_brief_card(held_tickers: set[str] | None = None) -> None:
    """Render the morning-brief Overview card. Safe to call on every rerun.

    Imports the live brief module lazily so this shared component is import-safe
    from entrypoints that don't have ``live/`` on sys.path.
    """
    from morning_brief import get_or_generate_brief  # live/ module (on path live)
    from morning_brief_cadence import Decision

    info = get_or_generate_brief(held_tickers=held_tickers)

    if not info["enabled"]:
        # Regulatory kill switch is off — render nothing.
        return

    brief = info.get("brief_text")
    as_of = info.get("as_of_et")
    closed = info["decision"] is Decision.CLOSED

    st.markdown("### 📋 Morning Brief")

    if brief:
        if closed:
            st.caption(
                f"🔴 Market closed — last brief as of {as_of}."
                if as_of else "🔴 Market closed."
            )
        elif info.get("stale_day"):
            st.caption(f"Last brief as of {as_of} (prior session).")
        else:
            st.caption(f"As of {as_of}." if as_of else "")
        st.markdown(brief)
    elif closed:
        st.caption("🔴 Market closed — today's brief will appear once the market opens.")
    else:
        st.caption("Preparing today's brief…")

    st.caption(
        "Auto-generated market context for orientation only — not investment "
        "advice."
    )
    st.divider()
