"""
Nous Ergon — Incidents & Retros

List-then-detail UX matching the blog. Default view shows a clickable
list of retros with date + severity + 1-line summary; clicking a title
navigates to ``?retro=<slug>`` and renders the full markdown.
"""

import html
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

from components.header import render_header, render_footer
from components.styles import inject_base_css, inject_docs_css

st.set_page_config(
    page_title="Retros — Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_base_css()
inject_docs_css()
render_header(current_page="Retros")

st.divider()

_RETROS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "retros")

# Order matters — top-to-bottom on the index. Curated as case studies,
# not a chronological feed; each retro is in a different failure domain.
_RETROS = [
    {
        "slug": "01_pfe_short_sell",
        "title": "PFE short-sell — accidental short on a long-only system",
        "date": "2026-04-22",
        "severity": "P1",
        "domain": "Trade execution",
        "summary": (
            "The executor opened a short position on a stock the system "
            "was only supposed to be exiting. Defense-in-depth fix shipped "
            "same day."
        ),
    },
    {
        "slug": "02_eod_pipeline_recovery",
        "title": "EOD pipeline recovery — 50 minutes to 4.5 minutes",
        "date": "2026-05-01",
        "severity": "P2",
        "domain": "Infrastructure",
        "summary": (
            "Three independent issues stacked into a 10× runtime regression. "
            "Closing them the same day cut runtime by an order of magnitude "
            "and propagated a defensive pattern to other parts of the data "
            "layer."
        ),
    },
    {
        "slug": "03_predictor_meta_collapse",
        "title": "Predictor meta-model collapse — 27 UP / 0 DOWN",
        "date": "2026-04-28",
        "severity": "P1",
        "domain": "ML model",
        "summary": (
            "A weekly retrain produced a degenerate output distribution. "
            "Root cause was placeholder constants in production training "
            "data; the fix moved validation IC from 0.053 to 0.132."
        ),
    },
]
_BY_SLUG = {r["slug"]: r for r in _RETROS}


# ---------------------------------------------------------------------------
# Routing — query param decides index vs detail view
# ---------------------------------------------------------------------------

params = st.query_params
selected_slug = params.get("retro")

if selected_slug and selected_slug in _BY_SLUG:
    # ── Detail view ────────────────────────────────────────────────────────
    st.markdown(
        '<a href="/Retros" target="_self" '
        'style="color: #1a73e8; text-decoration: none; font-size: 14px;">'
        '&larr; Back to retros</a>',
        unsafe_allow_html=True,
    )
    st.markdown("")  # spacer

    md_path = os.path.join(_RETROS_DIR, f"{selected_slug}.md")
    if os.path.exists(md_path):
        with open(md_path) as f:
            st.markdown(f.read())
    else:
        st.warning(f"Retro not found: {selected_slug}")

else:
    # ── Index view ─────────────────────────────────────────────────────────
    st.markdown("# Incidents & Retros")
    st.markdown(
        "Production maturity is easier to claim than to demonstrate. These "
        "are real incidents from the system, written tight: what failed, "
        "how it was caught, what caused it, what fixed it, and what changed "
        "structurally so the same class of bug doesn't recur."
    )
    st.markdown(
        "_The public set is curated as case studies, not a chronological "
        "feed. Three retros across three different failure domains; the "
        "private interview kit holds deeper retros with full code paths "
        "and naive first attempts._"
    )
    st.divider()

    for retro in _RETROS:
        slug = retro["slug"]
        title = html.escape(retro["title"])
        date = html.escape(retro["date"])
        severity = html.escape(retro["severity"])
        domain = html.escape(retro["domain"])
        summary = html.escape(retro["summary"])

        st.markdown(
            f"""
            <div style="margin: 8px 0 28px 0;">
                <a href="?retro={slug}" target="_self"
                   style="color: #1a73e8; text-decoration: none;
                          font-size: 22px; font-weight: 600;
                          line-height: 1.3;">
                    {title}
                </a>
                <div style="color: #888; font-size: 13px; margin: 6px 0 8px 0;">
                    {date} &middot; {severity} &middot; {domain}
                </div>
                <div style="color: #ccc; font-size: 14px; line-height: 1.55;">
                    {summary}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

render_footer()
