"""Uptime KPI block — primary metric for the Reliability + Evaluation phase.

Renders the rolling uptime %, a progress bar toward the 99% target, and
supporting breakdown numbers (sessions counted, minutes up).
"""

from __future__ import annotations

import streamlit as st

_TARGET_PCT = 99.0


def _aggregate(records: list[dict]) -> dict:
    """Sum across the rolling window. Returns uptime%, connected, market, sessions."""
    connected = sum(r.get("connected_minutes", 0) for r in records)
    market = sum(r.get("market_minutes", 0) for r in records)
    uptime_pct = (connected / market * 100.0) if market else 0.0
    return {
        "uptime_pct": uptime_pct,
        "connected_minutes": connected,
        "market_minutes": market,
        "sessions": len(records),
    }


def _progress_bar_html(pct: float) -> str:
    """Render a labeled progress bar with the 99% target tick."""
    pct_clamped = max(0.0, min(100.0, pct))
    bar_color = "#5fa8f0" if pct < _TARGET_PCT else "#7fd17f"
    return (
        f'<div style="position:relative; height:28px; background:#222; '
        f'border-radius:4px; overflow:hidden; margin:8px 0;">'
        f'<div style="width:{pct_clamped:.1f}%; height:100%; background:{bar_color};"></div>'
        f'<div style="position:absolute; top:0; left:{_TARGET_PCT}%; '
        f'height:100%; width:2px; background:#ff9; opacity:0.8;" '
        f'title="99% target"></div>'
        f'<div style="position:absolute; top:0; left:0; right:0; height:100%; '
        f'display:flex; align-items:center; justify-content:center; '
        f'color:#eee; font-weight:600; font-size:13px; text-shadow:0 0 4px #000;">'
        f'{pct:.1f}%  /  {_TARGET_PCT:.0f}% target'
        f'</div>'
        f'</div>'
    )


def render_uptime_kpi(records: list[dict]) -> None:
    """Render the reliability KPI block. Pass in the rolling-window records.

    Headings belong to the caller (System Pulse renders its own section
    header; the heading + "Phase 2 Primary KPI" caption that used to live
    here duplicated it and carried the hedge stripped in L4570a).
    """
    if not records:
        st.info("No uptime sessions recorded yet. Data will appear after the first post-tick-log EOD run.")
        return

    agg = _aggregate(records)
    st.caption(
        f"Rolling {agg['sessions']} market session"
        f"{'s' if agg['sessions'] != 1 else ''} "
        f"· NYSE 9:30-16:00 ET · Daemon up AND IB Gateway connected"
    )
    st.markdown(_progress_bar_html(agg["uptime_pct"]), unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Uptime", f"{agg['uptime_pct']:.1f}%", delta=f"target {_TARGET_PCT:.0f}%", delta_color="off")
    c2.metric("Sessions Counted", str(agg["sessions"]))
    c3.metric(
        "Minutes Up / Market Minutes",
        f"{agg['connected_minutes']:,} / {agg['market_minutes']:,}",
    )
