"""Phase indicator — shows the system's current engineering phase.

Alpha Engine moves through four phases. Each has a distinct primary KPI;
the homepage surfaces the current phase so visitors understand what's
being optimized now and why alpha is or isn't the headline metric.
"""

import streamlit as st

PHASES = [
    {"name": "Completeness", "status": "complete", "kpi": "Coverage"},
    {"name": "Reliability + Measurability", "status": "current", "kpi": "Uptime + Coverage"},
    {"name": "Performance (paper)", "status": "upcoming", "kpi": "Alpha vs SPY"},
    {"name": "Performance (live)", "status": "upcoming", "kpi": "NAV"},
]

# 2-3 bullet description per phase — rendered below the strip + caption.
# Authored directly here (Home is canonical; no separate snippet file).
_PHASE_DESCRIPTIONS = {
    "Completeness": [
        "6 modules wired end-to-end via S3 — research, prediction, execution, evaluation, data, dashboard.",
        "Multi-agent research + stacked meta-ensemble + risk-gated executor + autonomous backtester.",
        "Three Step Functions running unattended (Saturday weekly + weekday morning + EOD).",
    ],
    "Reliability + Measurability": [
        "Pipeline reliability — Step Functions reliable end-to-end with drift detection and runtime trend alarms.",
        "Every decision point measurable — agent calls, predictor verdicts, fills, P&L attribution, risk events.",
        "Autonomous feedback loop — backtester writing four optimized configs to S3 weekly.",
    ],
    "Performance (paper)": [
        "Operates the autonomous feedback loop on a Phase-2-trustworthy substrate.",
        "Broader feature breadth in inference (current 21 features &rarr; ~50-feature ArcticDB store).",
        "Gated on ≥ 99% SF success rate over 8 weeks + transparency-inventory complete.",
    ],
    "Performance (live)": [
        "Paper &rarr; live capital with progressive sizing.",
        "Portfolio-level risk overlays beyond per-position gates.",
        "Gated on sustained positive alpha vs SPY over a 12-week Phase 3 window.",
    ],
}

_COLORS = {
    "complete": {"bg": "#1e3a1e", "border": "#2d5a2d", "fg": "#7fd17f"},
    "current": {"bg": "#1a3a5a", "border": "#1a73e8", "fg": "#5fa8f0"},
    "upcoming": {"bg": "#2a2a2a", "border": "#444", "fg": "#888"},
}

_ICONS = {"complete": "&#x2713;", "current": "&#x25B6;", "upcoming": "&#x2022;"}


def render_phase_indicator(current_phase: str = "Reliability + Evaluation") -> None:
    """Render the four-phase pill row with the given phase highlighted."""
    pills = []
    for i, phase in enumerate(PHASES):
        # Override phases by position relative to the current one
        if phase["name"] == current_phase:
            status = "current"
        elif any(p["name"] == current_phase for p in PHASES[i + 1:]):
            status = "complete"
        else:
            status = "upcoming"

        c = _COLORS[status]
        icon = _ICONS[status]
        pill = (
            f'<div style="flex:1; min-width:140px; background:{c["bg"]}; '
            f'border:1px solid {c["border"]}; border-radius:6px; padding:10px 12px; '
            f'text-align:center;">'
            f'<div style="color:{c["fg"]}; font-size:11px; letter-spacing:1px; '
            f'text-transform:uppercase;">Phase {i + 1} {icon}</div>'
            f'<div style="color:#eee; font-weight:600; font-size:14px; margin-top:4px;">'
            f'{phase["name"]}</div>'
            f'<div style="color:#888; font-size:11px; margin-top:2px;">'
            f'KPI: {phase["kpi"]}</div>'
            f'</div>'
        )
        pills.append(pill)

    st.markdown(
        f"""
        <div style="display:flex; gap:8px; margin:12px 0 8px 0; flex-wrap:wrap;">
          {''.join(pills)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_phase_caption(current_phase: str = "Reliability + Measurability") -> None:
    """One-line explainer under the phase indicator."""
    captions = {
        "Completeness": "All six modules wired end-to-end — research, prediction, execution, evaluation, data, and dashboard.",
        "Reliability + Measurability": (
            "Every aspect of the system reliable and measurable, so Phase 3 "
            "can evaluate decisions on data, not vibes."
        ),
        "Performance (paper)": "Tuning signals, risk, and execution for sustained alpha vs SPY on paper.",
        "Performance (live)": "Graduating from paper to live capital with progressive sizing.",
    }
    text = captions.get(current_phase, "")
    if text:
        st.markdown(
            f'<div style="color:#aaa; font-size:13px; margin:4px 0 12px 0; '
            f'text-align:center; font-style:italic;">{text}</div>',
            unsafe_allow_html=True,
        )


def render_phase_descriptions(current_phase: str = "Reliability + Measurability") -> None:
    """Render four phase-description columns below the strip — 2-3 bullets each.

    Visually mirrors the strip layout so readers can connect each strip pill
    to its description below. Current phase highlighted with brand-blue
    accent; others in muted neutrals.
    """
    cards = []
    for i, phase in enumerate(PHASES):
        if phase["name"] == current_phase:
            status = "current"
        elif any(p["name"] == current_phase for p in PHASES[i + 1:]):
            status = "complete"
        else:
            status = "upcoming"

        c = _COLORS[status]
        bullets = _PHASE_DESCRIPTIONS.get(phase["name"], [])
        bullet_html = "".join(
            f'<li style="margin: 4px 0; line-height: 1.45;">{b}</li>'
            for b in bullets
        )

        card = (
            f'<div style="flex: 1; min-width: 200px; '
            f'background: rgba(255,255,255,0.02); '
            f'border: 1px solid {c["border"]}; '
            f'border-radius: 6px; padding: 12px 14px;">'
            f'<div style="color: {c["fg"]}; font-size: 12px; font-weight: 600; '
            f'letter-spacing: 0.5px; text-transform: uppercase; '
            f'margin-bottom: 8px;">Phase {i + 1} &middot; {phase["name"]}</div>'
            f'<ul style="color: #bbb; font-size: 13px; padding-left: 18px; '
            f'margin: 0;">{bullet_html}</ul>'
            f'</div>'
        )
        cards.append(card)

    st.markdown(
        f"""
        <div style="display: flex; gap: 10px; margin: 16px 0 8px 0;
                    flex-wrap: wrap;">
          {''.join(cards)}
        </div>
        """,
        unsafe_allow_html=True,
    )
