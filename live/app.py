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
    # Page file retained (unreachable) like performance below.
    # st.Page(os.path.join(_HERE, "pages", "uptime.py"), title="Uptime"),
    # Evaluation page (the legacy backtest/{date}/grading.json v1 letter
    # report card) removed from the public nav 2026-06-08, then DELETED
    # outright RC v3 T1 (config-I7474, 2026-08-16) — components/report_card.py
    # + pages/evaluation.py + loaders.s3_loader.load_latest_grading retired,
    # not left dormant (champion-challenger §6). The console's Report Card v2
    # (evaluator/{date}/report_card.json, 9 tiles) is the one card.
    # Performance page also removed from the public nav 2026-06-08 — it
    # publishes Cumulative Alpha vs S&P 500 + the NAV-vs-SPY chart, which
    # currently shows the portfolio underperforming SPY (Phase 2). Same
    # brand-surface logic as Evaluation above; page file retained for easy
    # re-enable once the system beats SPY (then it becomes a credibility flex).
    # st.Page(os.path.join(_HERE, "pages", "performance.py"), title="Performance"),
])

# Link-funnel (public-presence role matrix): this surface is the live
# proof-of-life tier; the narrative (what the system is, how it's designed)
# is owned by the Crucible product site (2026-06-12 restructure: the apex
# is the lab landing) — link out rather than re-tell it here.
with st.sidebar:
    st.caption(
        "What this system is and how it's designed: "
        "[crucible.nousergon.ai](https://crucible.nousergon.ai)"
    )

# Paper-trading disclaimer, rendered globally (before pg.run() = on every
# page, above page content) so a new page can't ship values without it.
st.caption(
    "**Paper trading** — all values shown are from a simulated Interactive "
    "Brokers paper account (nominal \\$1M start); no real money is traded. "
    "Nothing on this site is investment advice."
)

pg.run()
