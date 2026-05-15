"""Tests for components.artifact_archive — the reusable archive widget.

The component is purely presentational. These tests pin the
order-independent contract: newest-first ordering by ``sort_key``,
retention cap, latest rendered inline + priors via expanders, empty
state, and the expander-free render_fn invariant (render_fn is invoked
once per shown entry — same callable for latest + priors).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock streamlit so the component's st.* calls are no-ops we can assert
# against. Mirrors the repo-wide test convention (test_regime_*).
_mock_st = MagicMock()
_mock_st.cache_data = lambda **kw: (lambda f: f)
_mock_st.cache_resource = lambda **kw: (lambda f: f)


class _ExpanderCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_mock_st.expander = lambda *a, **kw: _ExpanderCtx()
sys.modules["streamlit"] = _mock_st

from components.artifact_archive import (  # noqa: E402
    ArchiveEntry,
    render_artifact_archive,
)


def _entries():
    return [
        ArchiveEntry("2026-05-13", "2605130200", {"d": 13}, "older"),
        ArchiveEntry("2026-05-15", "2605150200", {"d": 15}, "newest"),
        ArchiveEntry("2026-05-14", "2605140200", {"d": 14}, "mid"),
    ]


def test_renders_newest_first_and_calls_render_fn_per_entry():
    seen: list = []
    render_artifact_archive(
        title="T",
        description="D",
        entries=_entries(),
        render_fn=lambda p: seen.append(p["d"]),
    )
    # Latest (15) rendered inline first, then priors newest→oldest.
    assert seen == [15, 14, 13]


def test_retention_caps_entries():
    seen: list = []
    many = [
        ArchiveEntry(f"d{i}", f"260501{i:04d}", {"i": i}) for i in range(30)
    ]
    render_artifact_archive(
        title="T",
        description="D",
        entries=many,
        render_fn=lambda p: seen.append(p["i"]),
        retention_days=5,
    )
    # Newest 5 only (i=29..25), newest first.
    assert seen == [29, 28, 27, 26, 25]


def test_empty_entries_renders_message_not_render_fn():
    calls: list = []
    _mock_st.reset_mock()
    render_artifact_archive(
        title="T",
        description="D",
        entries=[],
        render_fn=lambda p: calls.append(p),
        empty_message="nothing here",
    )
    assert calls == []
    _mock_st.info.assert_called_once_with("nothing here")


def test_single_entry_has_no_priors_section():
    seen: list = []
    render_artifact_archive(
        title="T",
        description="D",
        entries=[ArchiveEntry("only", "2605150200", {"x": 1})],
        render_fn=lambda p: seen.append(p["x"]),
    )
    # Single entry → rendered once inline, no priors section.
    assert seen == [1]
