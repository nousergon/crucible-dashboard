"""
Generated Status — Alpha Engine Library (private console)

Browsable surface for ``alpha-engine-config/private-docs/STATUS_GENERATED.md``
— the machine-generated derived-state doc (lib-pin matrix, per-repo HEAD,
open PRs, lib/flow-doctor latest). Rendered as-is via ``st.markdown``.
Regenerated daily by the ``regenerate-status.yml`` GHA
(``python3 scripts/gen_status.py``) in alpha-engine-config; this page is a
read-only mirror, never a write path.

Part of the Library surface (config#2588).

**Loader:** ``loaders/system_docs_loader.py``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.system_docs_loader import STATUS_GENERATED_DOC, render_doc_tab

render_doc_tab(
    STATUS_GENERATED_DOC,
    title="Generated Status",
    caption=(
        "`alpha-engine-config/private-docs/STATUS_GENERATED.md` — derived "
        "state (lib-pin matrix, per-repo HEAD, open PRs), regenerated daily. "
        "Read-only mirror; never hand-edited here or in the source repo."
    ),
)
