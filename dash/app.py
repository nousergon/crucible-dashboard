"""Crucible /dash — the experiment-results surface (config#1957, plan §8.5).

The link-gated product skin of the Crucible Results pages: the SAME six
views the console hosts (shared ``results.view_model`` layer — one renderer,
two skins), served as their own Streamlit app under ``baseUrlPath=/dash`` on
port 8503 and exposed at https://crucible.nousergon.ai/dash via the
``crucible-live-proxy`` Worker, behind a Cloudflare Access email gate
(link-gated first; fully public only after a clean trust-battery month —
plan §8.5 / config#1958).

Run: ``systemd crucible-dash.service`` → ``streamlit run app.py`` with
WorkingDirectory = this ``dash/`` directory (so ``dash/.streamlit/config.toml``
— baseUrlPath + the corsAllowedOrigins WebSocket fix — is picked up).
``st.Page`` paths resolve relative to THIS file's directory, hence the
``../views/`` refs; the views' own ``sys.path`` bootstrap is CWD-independent.
"""
import streamlit as st

st.set_page_config(
    page_title="Crucible — Experiment Results",
    page_icon="⚗️",
    layout="wide",
)

st.navigation({
    "⚗ Crucible — Reference Rate": [
        st.Page("../views/Crucible_Overview.py", title="Overview", icon="🏛", default=True, url_path="overview"),
        st.Page("../views/Crucible_Validation.py", title="Validation", icon="🔬", url_path="validation"),
        st.Page("../views/Crucible_Evaluation.py", title="Evaluation", icon="⚖", url_path="evaluation"),
        st.Page("../views/Crucible_Execution.py", title="Execution", icon="⚡", url_path="execution"),
        st.Page("../views/Crucible_Feedback.py", title="Feedback loop", icon="🔁", url_path="feedback"),
        st.Page("../views/Crucible_Trust.py", title="Trust", icon="🛡", url_path="trust"),
    ],
}).run()
