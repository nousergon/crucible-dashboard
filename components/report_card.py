"""System Report Card — per-module letter grades from the weekly evaluator.

Structural quality data sourced from `backtest/{date}/grading.json`. Complements
the Uptime KPI: uptime answers "is the system running?", the report card
answers "is it running well?"

Deliberately conservative surface area: shows letter + numeric grade per
module, and letter + N/A reason per sub-component. Raw backing stats
(Sharpe, rank IC, alpha, hit rate) stay in the internal dashboard because
the Phase-2 sample is too small for them to be meaningfully interpreted
by outside observers.
"""

from __future__ import annotations

import streamlit as st

# Letter-grade palette. Greens/yellows/oranges tuned to sit on the dark
# background used by styles.py without looking neon.
_GRADE_COLORS = {
    "A": "#7fd17f",  # green — same tone as the at-target uptime bar
    "B": "#e0c050",  # muted yellow
    "C": "#e89050",  # orange
    "D": "#d06060",  # red
    "F": "#d06060",
    "N/A": "#666",
}

# Common acronyms that naive .title() mangles. Applied after word splitting.
_ACRONYMS = {
    "cio": "CIO",
    "gbm": "GBM",
    "vwap": "VWAP",
    "eod": "EOD",
    "spy": "SPY",
    "ic": "IC",
    "atr": "ATR",
    "sla": "SLA",
}


def _pretty_label(key: str) -> str:
    """Convert snake_case component keys into display labels, preserving acronyms."""
    parts = key.split("_")
    out = []
    for p in parts:
        lower = p.lower()
        if lower in _ACRONYMS:
            out.append(_ACRONYMS[lower])
        else:
            out.append(p.capitalize())
    return " ".join(out)


def _grade_color(letter: str | None) -> str:
    if not letter:
        return _GRADE_COLORS["N/A"]
    return _GRADE_COLORS.get(letter[0].upper(), _GRADE_COLORS["N/A"])


def _format_numeric(grade: float | int | None) -> str:
    if grade is None:
        return "—"
    return f"{grade:.0f}/100"


def _render_component_expander(module: dict) -> None:
    components = module.get("components", {}) or {}
    with st.expander("Component detail"):
        any_row = False
        for comp_key, comp in components.items():
            if comp_key == "sector_teams":
                # Array of team dicts — rolled up via sector_teams_avg.
                continue
            if not isinstance(comp, dict):
                continue
            letter = comp.get("letter", "N/A")
            color = _grade_color(letter)
            label = _pretty_label(comp_key)
            if letter == "N/A":
                reason = comp.get("reason") or "insufficient data"
                st.markdown(
                    f'<div style="color:#888; padding:4px 0;">'
                    f'{label} — <span style="color:{color};">N/A</span> '
                    f'· {reason}</div>',
                    unsafe_allow_html=True,
                )
            else:
                # Letter only — no backing metrics surfaced on the public site.
                st.markdown(
                    f'<div style="padding:4px 0;">'
                    f'{label} — <span style="color:{color}; font-weight:600;">'
                    f'{letter}</span></div>',
                    unsafe_allow_html=True,
                )
            any_row = True
        if not any_row:
            st.caption("No component detail reported for this module.")


def _render_tile(column, display_name: str, module: dict | None) -> None:
    with column:
        if not module:
            st.markdown(f"**{display_name}**")
            st.markdown(
                f'<div style="font-size:38px; color:{_GRADE_COLORS["N/A"]}; '
                f'font-weight:700; line-height:1;">—</div>',
                unsafe_allow_html=True,
            )
            st.caption("No grading data yet.")
            return
        letter = module.get("letter", "N/A")
        color = _grade_color(letter)
        grade = module.get("grade")
        st.markdown(f"**{display_name}**")
        st.markdown(
            f'<div style="font-size:38px; color:{color}; font-weight:700; '
            f'line-height:1;">{letter}</div>',
            unsafe_allow_html=True,
        )
        st.caption(_format_numeric(grade))
        _render_component_expander(module)


def render_report_card(grading: dict | None) -> None:
    """Render the full Report Card section."""
    st.markdown("### System Report Card — Phase 2 Baseline")
    st.caption(
        "Structural-quality grading from the weekly evaluator. "
        "Most sub-components show N/A while Phase 2 data accumulates — "
        "typically 4–8 weeks of signals before letter grades firm up."
    )

    if not grading:
        st.info("No evaluator grading has been published yet.")
        return

    overall = grading.get("overall") or {}
    overall_letter = overall.get("letter", "N/A")
    overall_numeric = _format_numeric(overall.get("grade"))
    overall_color = _grade_color(overall_letter)

    st.markdown(
        f'<div style="color:#ccc; margin-bottom:8px;">'
        f'Overall: <span style="color:{overall_color}; font-weight:700;">'
        f'{overall_letter}</span> ({overall_numeric})'
        f'</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    _render_tile(c1, "Research", grading.get("research"))
    _render_tile(c2, "Predictor", grading.get("predictor"))
    _render_tile(c3, "Executor", grading.get("executor"))

    run_date = grading.get("_run_date")
    if run_date:
        st.caption(f"Last evaluated {run_date}.")
