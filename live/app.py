"""
Nous Ergon — Live Dashboard
https://live.nousergon.ai/

Public read-only dashboard for the running Alpha Engine. The Astro apex
(nousergon.ai) owns the marketing/positioning narrative; this site is
where the charts and tables live.

Entry script is a thin router — page content lives under live/pages/
and is wired via st.navigation so sidebar labels and order are explicit
(legacy multipage would show this file as \"app\" in the sidebar).
"""

import os
import sys

# live/ has its own loaders/charts/ that shadow the console's top-level
# packages; append the repo root so the shared components/ widgets
# resolve at the top level while loaders.* / charts.* still resolve
# under live/.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

st.set_page_config(
    page_title="Nous Ergon — Live Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

_HERE = os.path.dirname(os.path.abspath(__file__))

pg = st.navigation([
    st.Page(
        os.path.join(_HERE, "pages", "holdings_and_trades.py"),
        title="Live Portfolio",
        default=True,
    ),
    st.Page(os.path.join(_HERE, "pages", "system_pulse.py"), title="System Pulse"),
    # Uptime page absorbed into System Pulse as its Reliability strip
    # (L4570e, 2026-06-09) — same substrate + renderer, one fewer nav stop.
    # Page file retained (unreachable) like evaluation/performance below.
    # st.Page(os.path.join(_HERE, "pages", "uptime.py"), title="Uptime"),
    # Evaluation page removed from the public nav 2026-06-08 — the legacy
    # backtest/{date}/grading.json report card (A–F, 3 modules) it renders is
    # superseded by the console's Report Card v2 (evaluator/{date}/report_card.json,
    # 7 tiles). Publishing thin-sample self-grades on a brand surface was all
    # downside; the page file is retained (unreachable) for easy re-enable.
    # st.Page(os.path.join(_HERE, "pages", "evaluation.py"), title="Evaluation"),
    # Performance page also removed from the public nav 2026-06-08 — it
    # publishes Cumulative Alpha vs S&P 500 + the NAV-vs-SPY chart, which
    # currently shows the portfolio underperforming SPY (Phase 2). Same
    # brand-surface logic as Evaluation above; page file retained for easy
    # re-enable once the system beats SPY (then it becomes a credibility flex).
    # st.Page(os.path.join(_HERE, "pages", "performance.py"), title="Performance"),
])

# Link-funnel (public-presence role matrix): this surface is the live
# proof-of-life tier; the narrative (what the system is, how it's designed)
# is owned by the Astro apex — link out rather than re-tell it here.
with st.sidebar:
    st.caption(
        "What this system is and how it's designed: "
        "[nousergon.ai](https://nousergon.ai)"
    )

pg.run()
