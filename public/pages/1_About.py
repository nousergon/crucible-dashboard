"""
Nous Ergon — About

Brand context page: brand origin (Nous Ergon = νοῦς ἔργον) + project
thesis + who built it + contact.

Per the presentation-layer outline (W2 spec), About owns brand context
only — *not* module descriptions. System architecture + per-pipeline
flows + per-module deep dives live on the Architecture page and the
per-repo GitHub READMEs respectively. Same fact in two places = two
staleness vectors.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

from components.header import render_header, render_footer
from components.styles import inject_base_css

st.set_page_config(
    page_title="About — Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_base_css()
render_header(current_page="About")

st.divider()

# ---------------------------------------------------------------------------
# Brand origin
# ---------------------------------------------------------------------------

st.markdown("### Nous Ergon")

st.markdown(
    """
    **Nous Ergon** — Greek for *intelligence at work* (νοῦς ἔργον,
    pronounced *noose air-gone*). *Nous* (νοῦς) is mind, intellect, the
    capacity for reason. *Ergon* (ἔργον) is work, deed, function — the
    same root as English *ergonomics* and *energy*.

    The name frames what the project is: agentic intelligence applied
    to a measurable, continuously verifiable problem. The work — the
    *ergon* — is what's on display.
    """
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Thesis
# ---------------------------------------------------------------------------

st.markdown("### Thesis")

st.markdown(
    """
    Build an experimentation harness for systematic equity strategies —
    multi-agent research, machine-learning prediction, risk-gated
    execution — and instrument every decision it makes end-to-end. The
    orchestration pattern consists of six modules collaborating through
    S3 contracts, three Step Function pipelines on a fixed cadence, and
    an autonomous feedback loop that writes optimized parameters back
    into the system.

    The harness is the durable artifact; alpha capture against the
    S&P 500 is the first experiment inside it. The system is currently
    in **Phase 2: Reliability + Measurability buildout** — making the
    instrument trustworthy enough that Phase 3 can turn alpha tuning on.
    Alpha is tracked, but not optimized, until measurement is
    trustworthy.

    See [Home](/) for live phase progress and per-phase key objectives,
    [Architecture](/Architecture) for the visual system walkthrough,
    and [Retros](/Retros) for production case studies.
    """
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Built by
# ---------------------------------------------------------------------------

st.markdown("### Built by")

st.markdown(
    """
    **Brian McMahon.** Single-developer project, in development since
    March 2026; **Claude Code** (Anthropic's LLM coding assistant) is
    the active collaborator on the implementation pass. Each module's
    repo is public.

    The project is a **harness for agentic-orchestration research**
    where different architectures, models, and patterns can be tested
    against measured baselines. Equities were chosen because financial
    data is abundant, decisions are unambiguous, and outcomes are
    continuously verifiable.

    Nous Ergon is also a demonstration of how AI-augmented solo
    engineering can scale.
    """
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Contact / Learn more
# ---------------------------------------------------------------------------

st.markdown("### Contact")

st.markdown(
    """
    - [LinkedIn](https://www.linkedin.com/in/brian-c-mcmahon/)
    - [brian@nousergon.ai](mailto:brian@nousergon.ai)
    - [GitHub](https://github.com/cipher813)
    - [Blog](https://nousergon.ai/blog)
    """
)

st.markdown("---")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

render_footer()
