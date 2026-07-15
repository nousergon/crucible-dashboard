"""Loader for the private-docs system-documentation corpus (Library page,
config#2588).

The corpus is the fleet's human-readable "what is true right now / why is
it shaped this way / what did we try" layer — ``SYSTEM_STATE.md`` +
per-repo ``system_state/*.md``, ``ARCHITECTURE.md``, ``EXPERIMENTS.md`` and
the generated ``STATUS_GENERATED.md``. It lives in the private
``alpha-engine-config`` repo, which is gitignored from the dashboard repo
but co-located on the EC2 console instance via
``infrastructure/boot-pull.sh`` (pulls ``/home/ec2-user/alpha-engine-config``
on every boot) — same arrangement ``loaders/observation_registry_loader.py``
already relies on for ``OBSERVATION_REGISTRY.yaml``. This loader copies that
exact 4-tier path-resolution pattern per source file instead of inventing a
new one.

Path resolution (in priority order), per relative path under
``private-docs/``:

  1. ``SYSTEM_DOCS_ROOT`` env var if set (test override) — treated as the
     ``private-docs/`` root, i.e. ``$SYSTEM_DOCS_ROOT/<relative_path>``.
  2. ``/home/ec2-user/alpha-engine-config/private-docs/<relative_path>``
     (the EC2 console path that ``boot-pull.sh`` populates).
  3. ``~/Development/alpha-engine-config/private-docs/<relative_path>``
     (local-dev path — sibling-directory layout under ``~/Development``).
  4. Sibling-relative:
     ``<dashboard-repo>/../alpha-engine-config/private-docs/<relative_path>``
     (catches non-default layouts where both repos are checked out as
     siblings under an arbitrary parent).

Markdown/YAML content is returned as raw text — the page renders it as-is
via ``st.markdown`` (or ``st.code`` for the YAML registries), no parsing.
Returns ``None`` if no path resolves — callers render a "not found" panel
rather than crashing the page. ``render_doc_tab`` is the shared render for
the single-file tabs (Architecture Doc / Experiments Log / Generated
Status) so each view script stays a 3-line spec, not a hand-rolled copy of
the load -> warn-if-missing -> caption+markdown sequence.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

# Tabs that surface a single top-level file (label -> path under private-docs/).
SYSTEM_STATE_INDEX = "SYSTEM_STATE.md"
ARCHITECTURE_DOC = "ARCHITECTURE.md"
EXPERIMENTS_DOC = "EXPERIMENTS.md"
STATUS_GENERATED_DOC = "STATUS_GENERATED.md"

# Per-repo axis files under system_state/ (label -> filename), in the same
# order SYSTEM_STATE.md's own index table lists them. Cross-repo files first
# (durable invariants + in-flight arcs), then one file per fleet repo.
SYSTEM_STATE_FILES: dict[str, str] = {
    "Cross-repo invariants": "cross_repo_invariants.md",
    "Cross-repo in-flight": "cross_repo_inflight.md",
    "Executor": "executor.md",
    "Research": "research.md",
    "Predictor": "predictor.md",
    "Backtester": "backtester.md",
    "Dashboard": "dashboard.md",
    "Data": "data.md",
    "Evaluator": "evaluator.md",
    "Lib": "lib.md",
    "Config": "config.md",
    "Docs": "docs.md",
}


def _candidate_roots() -> list[Path]:
    """``private-docs/`` roots to try, in priority order."""
    candidates: list[Path] = []

    env_override = os.environ.get("SYSTEM_DOCS_ROOT")
    if env_override:
        candidates.append(Path(env_override))

    candidates.append(Path("/home/ec2-user/alpha-engine-config/private-docs"))
    candidates.append(Path.home() / "Development" / "alpha-engine-config" / "private-docs")

    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "alpha-engine-config" / "private-docs")

    return candidates


def _resolve_path(relative_path: str) -> Path | None:
    for root in _candidate_roots():
        candidate = root / relative_path
        if candidate.exists():
            return candidate
    return None


@st.cache_data(ttl=900)
def load_doc(relative_path: str) -> dict[str, str] | None:
    """Read one private-docs file as text.

    Returns ``{"content": <text>, "source_path": <str>}`` or ``None`` if the
    file cannot be located/read on any of the 4 candidate roots.
    """
    path = _resolve_path(relative_path)
    if path is None:
        return None
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError subclass, not an OSError — a
        # non-UTF-8 byte in a generated doc (STATUS_GENERATED.md) must not
        # raise past this loader, or every Library tab page would render a
        # raw traceback instead of the intended "not reachable" panel.
        return None
    return {"content": content, "source_path": str(path)}


def load_system_state_index() -> dict[str, str] | None:
    """``SYSTEM_STATE.md`` — the thin index over ``system_state/*.md``."""
    return load_doc(SYSTEM_STATE_INDEX)


def load_system_state_file(filename: str) -> dict[str, str] | None:
    """One per-repo axis file, e.g. ``executor.md`` — pass the bare filename
    (see ``SYSTEM_STATE_FILES`` for the label -> filename map)."""
    return load_doc(f"system_state/{filename}")


def render_doc_tab(relative_path: str, *, title: str, caption: str) -> None:
    """Shared render for a single-file Library tab: title, caption, then
    either the rendered markdown or a "not reachable" warning.

    Used by the Architecture Doc / Experiments Log / Generated Status tabs
    (each a 3-line view script calling this with their own path/title/
    caption) so adding another single-file doc tab — e.g. the
    ``CONTRACT_REFERENCE_GENERATED.md`` / ``PIPELINE_DIAGRAMS_GENERATED.md``
    fast-follows referenced in config#2588 — never means re-pasting the
    load -> warn -> markdown sequence again.
    """
    st.title(title)
    st.caption(caption)

    doc = load_doc(relative_path)
    if doc is None:
        st.warning(
            f"`{relative_path}` not reachable from this instance — expected "
            "the alpha-engine-config repo to be co-located via boot-pull.sh "
            "(EC2) or as a `~/Development` / repo-sibling checkout (local "
            "dev).",
            icon="⚠️",
        )
    else:
        st.caption(f"Source: `{doc['source_path']}`")
        st.markdown(doc["content"])
