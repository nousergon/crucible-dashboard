"""Action Queue page contracts (config#3060 — split from the Decision Queue
per Brian's 2026-07-20 ruling: "if a decision/ruling leads to something I
have to do specifically, the decision queue is not the place to do that").

1. **Slug contract.** ``app.py`` MUST register ``views/50_Action_Queue.py``
   as a standalone ``st.Page`` with ``url_path="action-queue"``. Mirrors
   ``tests/test_decision_queue_page.py::TestSlugContract``.

2. **Home-chip contract.** ``app.py`` home renders the action-queue chip.

3. **Scope contract.** The page sources ``load_action_queue()``, not
   ``load_decision_queue()`` — a gate:decision item must never render here.

4. **Numbering.** Items render with a 1-based ``#N`` index — the numbered
   list Brian's ruling asked for, distinct from the Decision Queue's
   unnumbered oldest-first presentation.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

PAGE = REPO_ROOT / "views" / "50_Action_Queue.py"


class TestSlugContract:
    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_app_pins_action_queue_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'url_path="action-queue"' in app_src
        assert "views/50_Action_Queue.py" in app_src

    def test_home_renders_chip(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert "_render_action_queue_chip()" in app_src


class TestScopeContract:
    def test_page_sources_action_queue_not_decision_queue(self):
        src = PAGE.read_text()
        assert "load_action_queue" in src
        assert "load_decision_queue" not in src


class TestNumberedPresentation:
    def test_page_enumerates_with_a_1_based_index(self):
        src = PAGE.read_text()
        assert "enumerate(pending, start=1)" in src
        assert "index=i" in src

    def test_card_component_renders_the_index_prefix(self):
        src = (REPO_ROOT / "components" / "gate_queue_card.py").read_text()
        assert "index" in src
        assert '#{index}' in src


class TestActionFraming:
    """Unframed items default to 'mark done', not 'post a ruling' — the
    Action Queue's whole point is that the ruling was already made; what's
    outstanding is doing the thing."""

    def test_page_passes_is_action_true(self):
        src = PAGE.read_text()
        assert "is_action=True" in src

    def test_card_component_swaps_button_label_for_actions(self):
        src = (REPO_ROOT / "components" / "gate_queue_card.py").read_text()
        assert "Mark done" in src
