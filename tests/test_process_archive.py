"""Tests for the Item 5 per-process archive driver + dated lister.

list_dated_artifact_keys wiring (date extraction, basename/suffix
filter, latest.json exclusion, newest-first cap) + render_process_archive
reader dispatch. Mirrors the repo test convention (mock streamlit at
import, reload the real loaders module).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))

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


@pytest.fixture
def loader():
    import importlib
    for mod_name in ("loaders.s3_loader", "loaders"):
        cached = sys.modules.get(mod_name)
        if cached is not None and isinstance(cached, MagicMock):
            del sys.modules[mod_name]
    import loaders.s3_loader as s3_loader
    importlib.reload(s3_loader)
    return s3_loader


@pytest.fixture
def pa():
    """Real components.process_archive bound to _mock_st.

    Other test files replace sys.modules entries (streamlit / loaders /
    components) with un-restored MagicMocks. Re-pin streamlit to our
    mock, drop any MagicMock-poisoned module, and reload the dependency
    chain (artifact_archive → process_archive) so render dispatch is
    exercised against the real code. Mirrors the `loader` fixture's
    defensive pattern (documented repo-wide hazard).
    """
    import importlib

    sys.modules["streamlit"] = _mock_st
    for mod_name in (
        "components.process_archive",
        "components.artifact_archive",
        "loaders.s3_loader",
        "loaders",
        "components",
    ):
        cached = sys.modules.get(mod_name)
        if cached is not None and isinstance(cached, MagicMock):
            del sys.modules[mod_name]
    import components.artifact_archive as aa
    import components.process_archive as pa_mod
    importlib.reload(aa)
    importlib.reload(pa_mod)
    _mock_st.reset_mock()
    return pa_mod


def _paginator(keys):
    pag = MagicMock()
    pag.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    client = MagicMock()
    client.get_paginator.return_value = pag
    return client


# ── list_dated_artifact_keys ─────────────────────────────────────────────


def test_extracts_dates_filters_basename_excludes_latest(loader):
    keys = [
        "consolidated/2026-05-15/morning.md",
        "consolidated/2026-05-14/morning.md",
        "consolidated/2026-05-15/eod.html",   # wrong basename
        "consolidated/latest/morning.md",      # no date token
    ]
    with patch.object(loader, "get_s3_client", return_value=_paginator(keys)):
        with patch.object(loader, "_research_bucket", return_value="b"):
            out = loader.list_dated_artifact_keys(
                "consolidated/", basename="morning.md"
            )
    assert out == [
        ("2026-05-15", "consolidated/2026-05-15/morning.md"),
        ("2026-05-14", "consolidated/2026-05-14/morning.md"),
    ]


def test_suffix_filter_excludes_non_dated_sidecar(loader):
    keys = [
        "predictor/predictions/2026-05-15.json",
        "predictor/predictions/2026-05-13.json",
        "predictor/predictions/latest.json",   # no date → excluded
        "predictor/predictions/2026-05-15.txt",  # wrong suffix
    ]
    with patch.object(loader, "get_s3_client", return_value=_paginator(keys)):
        with patch.object(loader, "_research_bucket", return_value="b"):
            out = loader.list_dated_artifact_keys(
                "predictor/predictions/", suffix=".json"
            )
    assert [d for d, _ in out] == ["2026-05-15", "2026-05-13"]


def test_caps_to_n_recent_newest_first(loader):
    keys = [f"backtest/2026-05-{d:02d}/report.md" for d in range(1, 20)]
    with patch.object(loader, "get_s3_client", return_value=_paginator(keys)):
        with patch.object(loader, "_research_bucket", return_value="b"):
            out = loader.list_dated_artifact_keys(
                "backtest/", basename="report.md", n_recent=3
            )
    assert [d for d, _ in out] == ["2026-05-19", "2026-05-18", "2026-05-17"]


def test_empty_on_failure(loader):
    bad = MagicMock()
    bad.get_paginator.side_effect = RuntimeError("boom")
    with patch.object(loader, "get_s3_client", return_value=bad):
        with patch.object(loader, "_research_bucket", return_value="b"):
            out = loader.list_dated_artifact_keys("x/", basename="y")
    assert out == []


# ── render_process_archive reader dispatch ───────────────────────────────


def test_markdown_reader_renders_via_st_markdown(pa):
    spec = pa.ProcessArchiveSpec(
        title="T", description="D", list_prefix="consolidated/",
        basename="morning.md", reader="markdown",
    )
    with patch.object(
        pa, "list_dated_artifact_keys",
        return_value=[("2026-05-15", "consolidated/2026-05-15/morning.md")],
    ):
        with patch.object(
            pa, "download_s3_text", return_value="# Briefing\nbody"
        ):
            pa.render_process_archive(spec)
    _mock_st.markdown.assert_any_call("# Briefing\nbody")


def test_json_reader_renders_via_st_json(pa):
    spec = pa.ProcessArchiveSpec(
        title="T", description="D", list_prefix="predictor/predictions/",
        suffix=".json", reader="json",
    )
    payload = {"predictions": [{"ticker": "AAPL"}]}
    with patch.object(
        pa, "list_dated_artifact_keys",
        return_value=[("2026-05-15", "predictor/predictions/2026-05-15.json")],
    ):
        with patch.object(pa, "download_s3_json", return_value=payload):
            pa.render_process_archive(spec)
    _mock_st.json.assert_any_call(payload)


def test_empty_keys_renders_empty_message_not_loader(pa):
    spec = pa.ProcessArchiveSpec(
        title="T", description="D", list_prefix="x/", basename="y",
        reader="markdown", empty_message="nothing archived",
    )
    with patch.object(pa, "list_dated_artifact_keys", return_value=[]):
        with patch.object(pa, "download_s3_text") as dl:
            pa.render_process_archive(spec)
            dl.assert_not_called()
    _mock_st.info.assert_called_once_with("nothing archived")
