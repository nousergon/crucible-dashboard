"""
Architecture Doc — Alpha Engine Library (private console)

Browsable surface for ``alpha-engine-config/private-docs/ARCHITECTURE.md`` —
the durable, load-bearing design-principles doc ("WHY is the system shaped
this way"). Rendered as-is via ``st.markdown``; not to be confused with
``views/10_Architecture.py`` (the Reference host's diagrams + module-card
walkthrough) — that page is a bird's-eye visual tour, this tab is the full
underlying design-rationale document it points readers at.

Part of the Library surface (config#2588).

**Loader:** ``loaders/system_docs_loader.py``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.system_docs_loader import ARCHITECTURE_DOC, render_doc_tab

render_doc_tab(
    ARCHITECTURE_DOC,
    title="Architecture",
    caption=(
        "`alpha-engine-config/private-docs/ARCHITECTURE.md` — durable design "
        "principles and why the system is shaped this way. Read-only mirror; "
        "edit it in the alpha-engine-config repo, not here."
    ),
)
