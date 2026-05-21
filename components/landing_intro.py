"""Landing-page intro for the Nous Ergon public site.

Renders the narrative blocks that frame the home page: a hero one-liner,
a mission paragraph, and four high-level capability pillars. Lives above
the live system panels (phase indicator, uptime, performance) so
recruiters and skim-readers see positioning before receipts.
"""

import streamlit as st


_HERO_ONELINER = (
    "An experimentation harness for systematic equity strategies — "
    "multi-agent research, machine-learning prediction, and risk-gated "
    "execution, instrumented end-to-end from signal to P&amp;L. The first "
    "experiment is alpha capture against the S&amp;P 500."
)

_MISSION = (
    "Research agents scan the US large- and mid-cap universe each week. "
    "A machine-learning ensemble predicts short-term moves. "
    "A risk-gated executor places trades. A weekly backtester evaluates "
    "the system's own outputs and computes parameter updates that flow "
    "back into the next run — closing a feedback loop the system is being "
    "engineered to operate without manual intervention."
)

_PROVING_GROUND = (
    "Equities are the proving ground: decisions, outcomes, and P&amp;L are "
    "unambiguous and continuously verifiable. The orchestration, "
    "measurement, and learning loops generalize to any domain where "
    "multi-agent collaboration and end-to-end measurement matter."
)

_PILLARS = [
    (
        "Multi-agent orchestration",
        "Research teams, a portfolio-level decision agent, and a macro "
        "layer collaborating weekly on a LangGraph + Claude stack; "
        "structured outputs and rubric-based LLM-as-judge throughout.",
    ),
    (
        "Machine-learning overlay",
        "A stacked ensemble of gradient-boosted and linear models "
        "producing market-relative return predictions and "
        "confidence-driven veto signals.",
    ),
    (
        "Self-improvement loop",
        "Weekly evaluation of the system's own outputs writes parameter "
        "updates back into four S3 configs that downstream modules read on "
        "cold-start — the system retunes itself without manual intervention.",
    ),
    (
        "End-to-end measurement",
        "Every signal, prediction, fill, and dollar of P&amp;L instrumented "
        "and traceable; the dashboard is a view, not a measurement layer.",
    ),
]


def render_landing_intro() -> None:
    """Render hero one-liner + mission paragraph + four pillars."""
    st.markdown(
        f"""
        <div style="text-align: center; max-width: 820px; margin: 28px auto 8px auto;">
            <p style="font-size: 18px; color: #ddd; line-height: 1.55; margin: 0;">
                {_HERO_ONELINER}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <div style="max-width: 820px; margin: 24px auto 8px auto;">
            <p style="font-size: 15px; color: #bbb; line-height: 1.65; margin: 0 0 14px 0;">
                {_MISSION}
            </p>
            <p style="font-size: 15px; color: #bbb; line-height: 1.65; margin: 0;">
                {_PROVING_GROUND}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    cards_html = "".join(
        f"""
        <div style="background: rgba(255,255,255,0.03);
                    border: 1px solid rgba(255,255,255,0.08);
                    border-radius: 8px;
                    padding: 16px 18px;">
            <div style="color: #1a73e8; font-size: 13px; font-weight: 600;
                        letter-spacing: 0.5px; margin-bottom: 8px;
                        text-transform: uppercase;">
                {title}
            </div>
            <div style="color: #ccc; font-size: 14px; line-height: 1.5;">
                {body}
            </div>
        </div>
        """
        for title, body in _PILLARS
    )

    st.markdown(
        f"""
        <div style="max-width: 980px; margin: 28px auto 8px auto;
                    display: grid; grid-template-columns: 1fr 1fr;
                    gap: 14px;">
            {cards_html}
        </div>
        """,
        unsafe_allow_html=True,
    )
