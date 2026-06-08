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
        title="Holdings & Trades",
        default=True,
    ),
    st.Page(os.path.join(_HERE, "pages", "uptime.py"), title="Uptime"),
    # Evaluation page removed from the public nav 2026-06-08 — the legacy
    # backtest/{date}/grading.json report card (A–F, 3 modules) it renders is
    # superseded by the console's Report Card v2 (evaluator/{date}/report_card.json,
    # 7 tiles). Publishing thin-sample self-grades on a brand surface was all
    # downside; the page file is retained (unreachable) for easy re-enable.
    # st.Page(os.path.join(_HERE, "pages", "evaluation.py"), title="Evaluation"),
    st.Page(os.path.join(_HERE, "pages", "performance.py"), title="Performance"),
])
pg.run()
