"""Every pipeline-status ArchivePageRef must resolve to a LIVE console URL.

The chokepoint the 6/29 nav collapse was missing: nousergon-lib's
``pipeline_status.registry`` deep-links page-25 cells at console URL paths,
and nothing pinned those values against the dashboard's real nav — so page
renames/retirements silently 404'd the links (found in the console-IA audit,
alpha-engine-config#1990; contract restated in lib v0.96.0's ArchivePageRef
docstring). This guard walks the INSTALLED lib registry and asserts every
``page`` value is either:

  * a registered ``st.Page`` slug in app.py — a pinned ``url_path=`` or the
    filename-derived default (stem, with and without the numeric prefix), or
  * a lazy-host tab deep-link ``host_<x>?tab=<label>`` whose host file exists
    and registers exactly that tab label.

Mirrors ``TestDeepLinkTargets`` in test_fleet_status_page.py (the same
guard for the Fleet Status page's own links).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nousergon_lib.pipeline_status.registry import (  # noqa: E402
    STATE_TO_ARCHIVE_PAGE,
    ArchivePageRef,
)

REPO_ROOT = Path(__file__).parent.parent
VIEWS = REPO_ROOT / "views"


def _registered_slugs() -> set[str]:
    """Slugs reachable as /<slug> per app.py's st.navigation registration."""
    app_src = (REPO_ROOT / "app.py").read_text()
    slugs = set(re.findall(r'url_path="([^"]+)"', app_src))
    # page("<file>.py", ...) and st.Page("views/<file>.py", ...) entries
    # without a url_path pin get a filename-derived default slug.
    for fname in re.findall(r'page\("([^"]+)\.py"', app_src) + re.findall(
        r'st\.Page\(\s*"views/([^"]+)\.py"', app_src
    ):
        slugs.add(fname)
        slugs.add(re.sub(r"^\d+_", "", fname))  # Streamlit strips the sort prefix
    return slugs


def _host_tab_labels(host_filename: str) -> set[str]:
    path = VIEWS / f"{host_filename}.py"
    if not path.exists():
        return set()
    return {m.group(1) for m in re.finditer(
        r'\(\s*"([^"]+)"\s*,\s*"[^"]+\.py"\s*\)', path.read_text()
    )}


def _archive_page_refs() -> list[tuple[str, str]]:
    return sorted(
        (state, entry.page)
        for state, entry in STATE_TO_ARCHIVE_PAGE.items()
        if isinstance(entry, ArchivePageRef)
    )


@pytest.mark.parametrize("state,page", _archive_page_refs())
def test_every_archive_page_ref_resolves(state, page):
    if "?tab=" in page:
        host, query = page.split("?", 1)
        tab = parse_qs(query).get("tab", [None])[0]
        assert tab is not None, f"{state}: malformed tab query in {page!r}"
        labels = _host_tab_labels(host)
        assert labels, f"{state}: host view {host!r} does not exist"
        assert tab in labels, (
            f"{state}: registry deep-links {page!r} but {host}.py registers "
            f"tabs {sorted(labels)} — update the lib registry entry (and bump "
            f"the lib pin) in lockstep with any tab rename."
        )
    else:
        slugs = _registered_slugs()
        assert page in slugs, (
            f"{state}: registry deep-links slug {page!r} which is not a "
            f"registered st.Page slug in app.py — update the lib registry "
            f"entry (and bump the lib pin) in lockstep with any page "
            f"rename/retirement."
        )


def test_registry_has_archive_page_refs():
    # Sanity: the parametrization isn't silently empty.
    assert len(_archive_page_refs()) >= 10
