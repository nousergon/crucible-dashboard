"""The bounded cache wrapper, as reached from the `live/` app root.

WHY THIS FILE EXISTS
--------------------
`nous-ergon-live.service` starts with `WorkingDirectory=.../alpha-engine-dashboard/live`
and runs `app.py` from there (verified on i-09b539c844515d549, 2026-07-31), so
Streamlit puts `live/` first on `sys.path` and the name `loaders` resolves to
`live/loaders`, NOT to the repo-root `loaders/` package. `dashboard.service`
roots at the repo root and gets the other one. Two apps, one checkout, the same
import name meaning different packages depending on which is running.

So `from loaders.cache import cached` — which alpha-engine-config#5270's first
pass added to four files under `live/loaders/` — resolves here at runtime, and
before this file existed it raised `ModuleNotFoundError` at import. That is not
a degraded page: the live app fails to start, and `nous-ergon-live.service` is
the PUBLIC site. Caught pre-merge by `tests/test_system_pulse_loader.py`, which
reproduces the runtime path exactly (drops the top-level `loaders` package and
inserts `live/`) rather than trusting the console's import graph.

WHY A LOADER SHIM AND NOT A COPY
--------------------------------
The sizing of `DEFAULT_MAX_ENTRIES` is a measured argument with a floor and a
ceiling (see the canonical module). Duplicating it here would fork that
reasoning immediately, and `shared-code-policy.md` §5 is explicit that a copy is
legitimate only while a test proves it still matches — a bar a second hand-
maintained copy of a rationale cannot clear for long.

The two trees ship in ONE checkout and are deployed together, so the canonical
module is always on disk at a fixed relative path. This loads it by that path
and re-exports it, which makes the live app and the console share one object
rather than two that agree today.

Fails LOUD if the canonical module is missing: a silent fallback to an
unbounded `st.cache_data` would restore exactly the unbounded footprint #5270
exists to remove, and it would do it invisibly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CANONICAL = Path(__file__).resolve().parent.parent.parent / "loaders" / "cache.py"

if not _CANONICAL.is_file():
    raise ImportError(
        f"canonical cache wrapper not found at {_CANONICAL}. The live app and "
        f"the console ship in one checkout and this shim re-exports the "
        f"console's module; if the trees were split, this file must become a "
        f"real implementation rather than silently falling back to an "
        f"unbounded st.cache_data (alpha-engine-config#5270)."
    )

_SPEC = importlib.util.spec_from_file_location("_console_loaders_cache", _CANONICAL)
_MODULE = importlib.util.module_from_spec(_SPEC)
# Registered under its own name so repeated imports across the live app reuse
# one module object, and so the module is importable from inside itself.
sys.modules.setdefault("_console_loaders_cache", _MODULE)
_SPEC.loader.exec_module(_MODULE)

cached = _MODULE.cached
DEFAULT_MAX_ENTRIES = _MODULE.DEFAULT_MAX_ENTRIES

__all__ = ["DEFAULT_MAX_ENTRIES", "cached"]
