"""Lazy multi-view host — render exactly ONE sub-view per page load.

The console IA collapses each module's drill-down pages into a single tabbed
front page. ``st.tabs`` renders every tab body eagerly (running each sub-view's
S3 reads / heatmap builds on every load), so instead a ``st.segmented_control``
selects ONE sub-view and we exec only that view's script — just the active
view's loaders run. The ``?tab=`` query param is preserved so a view is
bookmarkable / deep-linkable.

Sub-views keep working **unchanged** as ordinary Streamlit scripts: each is
exec'd top-to-bottom exactly as ``st.navigation`` would run a registered page
(``__file__`` is set so their ``sys.path`` bootstrap and relative loads resolve).
This avoids rewriting two dozen heterogeneous page files into ``render()``
functions — a much smaller regression surface for the same lazy-load + single-
entry-per-module outcome. Only one sub-view executes per run, so widget ``key=``
values never collide across sub-views.
"""
from __future__ import annotations

import importlib.util
import os

import streamlit as st

# shared/ lives directly under the repo root; views/ is its sibling.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VIEWS_DIR = os.path.join(_REPO_ROOT, "views")


def _exec_view(filename: str) -> None:
    """Execute a ``views/<filename>`` script in a fresh module each run.

    A fresh ``exec_module`` every call means the sub-view re-renders on every
    Streamlit rerun (interactivity preserved); its ``@st.cache_data`` loaders
    still hit cache because the cache key is the function's stable module name +
    qualname + source, not its object identity.
    """
    path = os.path.join(_VIEWS_DIR, filename)
    mod_name = "_subview_" + filename[:-3] if filename.endswith(".py") else filename
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        st.error(f"Sub-view {filename} could not be loaded.")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def render_host(subviews: list[tuple[str, str]], *, key: str) -> None:
    """Render a tabbed front page over ``subviews`` = ``[(label, filename), …]``.

    A ``segmented_control`` picks the active sub-view; only that view's script is
    exec'd. ``key`` must be unique per host page. The selection round-trips
    through ``?tab=`` so the view is bookmarkable.
    """
    labels = [label for label, _ in subviews]
    by_label = dict(subviews)
    qp_tab = st.query_params.get("tab")
    active = st.segmented_control(
        "View", labels,
        default=qp_tab if qp_tab in labels else labels[0],
        key=key,
    ) or labels[0]
    st.query_params["tab"] = active
    st.divider()
    _exec_view(by_label[active])
