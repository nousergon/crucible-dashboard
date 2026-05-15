"""
process_archive.py — Thin-config driver for per-process artifact pages.

ROADMAP Observability Item 5: one console page per email-emitting
process, each showing the last ~2 weeks of that process's persisted
artifact with the latest rendered inline. Built on the Item 4
``artifact_archive`` component (the template) — this driver makes each
process a thin page: declare a :class:`ProcessArchiveSpec`, call
:func:`render_process_archive`. Adding a new process = one spec.

Reader contract
---------------
Each spec names a ``reader`` for the producer's persisted format
(verified per-producer, not guessed):

- ``markdown`` — rendered email/report markdown (research ``morning.md``,
  backtester ``report.md``)
- ``html``     — rendered email HTML (executor ``eod.html``)
- ``json``     — structured artifact (predictor ``predictions/{date}.json``,
  ``training_summary_{date}.json``)

The driver loads each dated key via the matching cached loader in
``loaders/s3_loader.py`` and renders through the shared archive widget.
``render_fn`` stays expander-free (artifact_archive nests history in
expanders; Streamlit forbids nested expanders).
"""
from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from components.artifact_archive import ArchiveEntry, render_artifact_archive
from loaders.s3_loader import (
    download_s3_json,
    download_s3_text,
    list_dated_artifact_keys,
)


@dataclass(frozen=True)
class ProcessArchiveSpec:
    """One email-emitting process's archive configuration.

    Attributes:
        title / description: page heading + caption.
        list_prefix: S3 prefix to enumerate (research bucket).
        reader: ``"markdown"`` | ``"html"`` | ``"json"``.
        basename / suffix: key filter (e.g. ``"morning.md"`` or
            ``".json"``) — the date-token requirement in
            ``list_dated_artifact_keys`` already excludes ``latest.json``
            sidecars, ``basename``/``suffix`` disambiguate co-located
            files.
        empty_message: shown pre-deploy / when nothing is persisted.
        retention_days: archive depth (weekly producers pass a smaller
            value than the 14-default daily cadence).
    """

    title: str
    description: str
    list_prefix: str
    reader: str
    basename: str | None = None
    suffix: str | None = None
    empty_message: str = (
        "No artifacts persisted yet — this surface populates once the "
        "producing process next runs."
    )
    retention_days: int = 14
    bucket: str = "alpha-engine-research"


def _render_markdown(payload: str) -> None:
    if payload:
        st.markdown(payload)
    else:
        st.info("Artifact present but empty.")


def _render_html(payload: str) -> None:
    if payload:
        # Email HTML — render in an isolated iframe so its styles don't
        # leak into the console chrome.
        st.components.v1.html(payload, height=900, scrolling=True)
    else:
        st.info("Artifact present but empty.")


def _render_json(payload: dict) -> None:
    if payload:
        st.json(payload)
    else:
        st.info("Artifact present but empty.")


def render_process_archive(spec: ProcessArchiveSpec) -> None:
    """Render a full per-process artifact-archive page from one spec."""
    keys = list_dated_artifact_keys(
        spec.list_prefix,
        basename=spec.basename,
        suffix=spec.suffix,
        n_recent=spec.retention_days,
    )

    entries: list[ArchiveEntry] = []
    for date_str, key in keys:
        if spec.reader in ("markdown", "html"):
            payload = download_s3_text(spec.bucket, key)
        else:  # json
            payload = download_s3_json(spec.bucket, key)
        if payload is None:
            continue
        entries.append(
            ArchiveEntry(label=date_str, sort_key=date_str, payload=payload)
        )

    render_fn = {
        "markdown": _render_markdown,
        "html": _render_html,
        "json": _render_json,
    }[spec.reader]

    render_artifact_archive(
        title=spec.title,
        description=spec.description,
        entries=entries,
        render_fn=render_fn,
        retention_days=spec.retention_days,
        empty_message=spec.empty_message,
    )
