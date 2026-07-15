"""
Experiments Log — Alpha Engine Library (private console)

Browsable surface for ``alpha-engine-config/private-docs/EXPERIMENTS.md`` —
the append-only log of what was tried and learned, including negative
results. Rendered as-is via ``st.markdown``. Not to be confused with
``views/46_Experiments.py`` (the live champion/challenger ablation
leaderboards under the Experiments nav section) — that page reads scored S3
artifacts for in-flight observe-only substrates; this tab is the narrative
experiment ledger (including work that never shipped).

Part of the Library surface (config#2588).

**Loader:** ``loaders/system_docs_loader.py``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.system_docs_loader import EXPERIMENTS_DOC, render_doc_tab

render_doc_tab(
    EXPERIMENTS_DOC,
    title="Experiments Log",
    caption=(
        "`alpha-engine-config/private-docs/EXPERIMENTS.md` — append-only log "
        "of what was tried and learned, especially negative results. "
        "Read-only mirror; edit it in the alpha-engine-config repo, not here."
    ),
)
