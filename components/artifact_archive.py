"""
artifact_archive.py — Reusable "latest inline + dated history" surface.

The shared console widget behind both the per-ticker order-book
rationale page (ROADMAP Observability Item 4) and the per-process
artifact-archive pages (Item 5). Each process becomes a thin page:
fetch a list of :class:`ArchiveEntry` from a cached loader, supply a
``render_fn`` for one artifact, and call :func:`render_artifact_archive`.

Contract / Streamlit constraint
-------------------------------
History entries are rendered inside ``st.expander``. Streamlit forbids
**nested expanders**, so ``render_fn`` MUST NOT itself open an
``st.expander`` (use tables / selectboxes / columns for drill-down
instead). The same ``render_fn`` is used for the inline "latest" block
and every history entry, so keeping it expander-free guarantees both
render paths work.

The component is purely presentational — no S3, no caching. Loaders
(cached via ``st.cache_data``) live in ``loaders/s3_loader.py``; pages
wire loader → entries → ``render_fn``. This keeps the Streamlit
idiom clean (cached loaders, dumb components) and makes the widget
reusable across canonical ``eval_artifacts`` producers and legacy
``{date}``-partitioned artifacts alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import streamlit as st


@dataclass
class ArchiveEntry:
    """One artifact in the archive.

    Attributes:
        label: human date label for the history list / latest header
            (e.g. ``"2026-05-15 (Thu)"``).
        sort_key: orders the archive newest-first. Use the canonical
            run_id (``YYMMDDHHMM``) or an ISO date — anything that
            lexicographically sorts chronologically.
        payload: the already-loaded artifact passed straight to
            ``render_fn`` (a dict, a markdown string, a DataFrame — the
            renderer's contract, not the component's).
        summary: optional one-line caption shown next to the date in
            the history list and under the latest header (e.g.
            ``"42 considered · 4 entries · 2 vetoed"``).
    """

    label: str
    sort_key: str
    payload: Any
    summary: str | None = None


def render_artifact_archive(
    *,
    title: str,
    description: str,
    entries: list[ArchiveEntry],
    render_fn: Callable[[Any], None],
    retention_days: int = 14,
    empty_message: str = "No artifacts available yet — "
    "this surface populates once the producer next runs.",
) -> None:
    """Render the latest artifact inline + a dated history list.

    Args:
        title: section heading.
        description: one-line ``st.caption`` under the heading.
        entries: artifacts to show. Order-independent — sorted
            newest-first by ``sort_key`` here.
        render_fn: renders a single ``entry.payload``. Must be
            expander-free (see module docstring).
        retention_days: cap on the most-recent artifacts retained
            (one artifact/day cadence assumed; weekly producers pass a
            smaller value). The latest is always rendered inline; up to
            ``retention_days - 1`` priors are click-to-expand.
        empty_message: shown when ``entries`` is empty (pre-deploy /
            producer hasn't run yet — a graceful state, not an error).
    """
    st.markdown(f"### {title}")
    st.caption(description)
    st.divider()

    if not entries:
        st.info(empty_message)
        return

    ordered = sorted(entries, key=lambda e: e.sort_key, reverse=True)[
        :retention_days
    ]
    latest = ordered[0]

    st.markdown(f"#### Latest — {latest.label}")
    if latest.summary:
        st.caption(latest.summary)
    render_fn(latest.payload)

    priors = ordered[1:]
    if priors:
        st.divider()
        st.markdown(f"#### Past {len(priors)} {'day' if len(priors) == 1 else 'days'}")
        for entry in priors:
            header = entry.label
            if entry.summary:
                header = f"{entry.label} — {entry.summary}"
            with st.expander(header):
                render_fn(entry.payload)
