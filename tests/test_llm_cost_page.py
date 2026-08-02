"""Source-assertion contracts for the API (LLM Cost) page's per-source
breakdown — mirrors tests/test_expenses_page.py's wiring-test style.

The per-source breakdown (PR: feat/llm-cost-per-source-groom-breakdown) lifts
the ``source`` dimension the headline aggregates away, so the groom runs' cost
is visible on the cost page rather than only the Backlog Groom page. These
tests assert the view imports the helpers and renders the section, without
needing a streamlit runtime.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "23_LLM_Cost.py"

SRC = PAGE.read_text()


def test_page_imports_source_breakdown_helpers():
    """The per-source subsection depends on the pure helpers in
    shared/usage_source_view.py — asserting the import here catches a
    rename or removal that would break the section at runtime."""
    assert "from shared.usage_source_view import" in SRC
    assert "source_breakdown" in SRC
    assert "daily_cost_by_source" in SRC


def test_page_renders_per_source_section():
    """The cost-by-source subsection is present and labeled, so the groom
    cost is distinguishable from interactive use on this page."""
    assert "Cost by source" in SRC
    # The cache-read % column is the cost-efficiency signal that justifies
    # the section — its presence is the contract that this page now answers
    # "how cache-efficient was each source", not just "what did it cost".
    assert "Cache read" in SRC


def test_source_breakdown_section_is_within_non_anthropic_block():
    """The subsection lives inside the non-Anthropic block (which holds the
    ``df_non_anthropic`` frame the helpers consume), not the Anthropic
    research-pipeline block below it. A misplaced insertion would reference
    a frame that doesn't exist at that point in the script."""
    na_block = SRC.index("Personal — non-Anthropic API cost")
    anth_block = SRC.index("Research pipeline — Anthropic API cost")
    src_section = SRC.index("Cost by source")
    assert na_block < src_section < anth_block
