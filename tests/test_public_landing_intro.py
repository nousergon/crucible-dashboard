"""Tests for public/components/landing_intro narrative content.

Locks the structural shape of the landing-page intro: the four pillars
exist, headlines stay on-message, and the agentic-engineering framing
isn't accidentally swapped back to a returns-first pitch.
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

pytest.importorskip("streamlit")

from components import landing_intro  # noqa: E402


def test_four_pillars_present():
    titles = [t for t, _ in landing_intro._PILLARS]
    assert len(titles) == 4
    assert "Multi-agent orchestration" in titles
    assert "Machine-learning overlay" in titles
    assert "Self-improvement loop" in titles
    assert "End-to-end measurement" in titles


def test_self_improvement_pillar_describes_mechanism_not_returns():
    body = dict(landing_intro._PILLARS)["Self-improvement loop"]
    # Autonomy is now a Phase 1 receipt (the backtester→S3-config feedback
    # loop is shipped); the pillar describes the mechanism rather than
    # framing autonomy as future work. Still must not lead with returns.
    forbidden = ["alpha", "outperform", "beating", "profit", "returns vs"]
    leaked = [t for t in forbidden if t in body.lower()]
    assert not leaked, (
        f"Self-improvement pillar must not lean on returns-flavored "
        f"framing; found: {leaked}"
    )
    # Honesty floor: the pillar should describe mechanism (configs / params /
    # parameter updates etc.), not claim outcomes (alpha generation).
    mechanism_words = ["config", "parameter", "tune", "evaluation", "loop"]
    assert any(w in body.lower() for w in mechanism_words), (
        "Self-improvement pillar must describe the mechanism (configs, "
        "parameter updates, evaluation loop), not just claim self-improvement."
    )


def test_hero_does_not_lead_with_returns_claims():
    # Under the harness-primary framing (2026-05-21 strategic decision),
    # naming the current experiment is declarative — "the first experiment
    # is alpha capture against the S&P 500" states what the harness is
    # *doing*, not a claim about returns. Outcome-claim words
    # ("outperform", "beating", etc.) stay forbidden; "alpha" as a noun
    # naming the experiment is permitted.
    text = (landing_intro._HERO_ONELINER + " " + landing_intro._MISSION).lower()
    forbidden = ["outperform", "beating", "returns vs", "profit", "alpha generation"]
    leaked = [term for term in forbidden if term in text]
    assert not leaked, (
        f"Landing copy must not claim returns or outperformance; "
        f"found: {leaked}"
    )


def test_hero_leads_with_harness_identity():
    # The hero one-liner must establish the harness/instrument identity
    # before naming the current experiment — guards against drifting back
    # to a "system that trades equities" framing that pre-dated the
    # 2026-05-21 strategic-framing decision (ROADMAP "Strategic Framing
    # — Two Products, Not One"). The harness is the durable product;
    # alpha capture is the validating problem domain.
    text = landing_intro._HERO_ONELINER.lower()
    identity_words = ["harness", "experiment", "instrument"]
    assert any(w in text for w in identity_words), (
        f"Hero must establish harness/instrument identity (one of "
        f"{identity_words!r}); current hero: {landing_intro._HERO_ONELINER!r}"
    )
