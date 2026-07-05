"""Tests for the Pipeline Status console page: pinned deep-link slug +
``?run=`` execution selection.

Two contracts guarded here:

1. **Slug contract.** The Step Function failure/complete notifications
   (nousergon-data) deep-link to
   ``https://console.nousergon.ai/pipeline-status?run=<execution-name>``
   via ``krepis.console.console_url``. So ``app.py`` MUST register
   ``views/25_Pipeline_Status.py`` as a standalone ``st.Page`` with
   ``url_path="pipeline-status"`` — a drift here silently breaks every
   emitted SF notification link. Mirrors ``tests/test_model_zoo_page.py`` /
   ``tests/test_director_page.py``.

2. **``?run=`` selection contract.** A ``?run=<execution-name>`` that matches
   an execution of a state machine selects THAT execution (keyed on the SF
   ``$$.Execution.Name`` == the trailing name segment of the execution ARN);
   a non-matching or absent ``?run=`` falls back to the canonical most-recent
   auto-pick. Never errors, never blank-screens.

The page is exec-loaded with ``streamlit`` mocked and the loader patched to
no-op (no network / no AWS), mirroring ``tests/test_book_status_banner.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Pinned slug — MUST equal the slug the SF notifications deep-link to
# (nousergon-data: console.nousergon.ai/pipeline-status?run=<execution-name>).
EXPECTED_SLUG = "pipeline-status"
PAGE = REPO_ROOT / "views" / "25_Pipeline_Status.py"


# ── 1 · Slug contract (static source assertions — no import needed) ─────────


class TestSlugContract:
    def test_app_pins_pipeline_status_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_pinned_page_is_the_pipeline_status_view(self):
        # The pinned url_path must sit on the Pipeline Status view file, not
        # on some other st.Page — otherwise the slug points at the wrong page.
        app_src = (REPO_ROOT / "app.py").read_text()
        # The st.Page(...) block naming the view and the url_path must be
        # co-located (same call). Cheap proximity check.
        idx = app_src.index("views/25_Pipeline_Status.py")
        window = app_src[idx : idx + 300]
        assert f'url_path="{EXPECTED_SLUG}"' in window

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_page_honors_run_query_param(self):
        # The deep-link is useless if the page ignores ?run=; assert the page
        # reads it, like the other pinned pages read ?date=.
        src = PAGE.read_text()
        assert 'st.query_params.get("run")' in src


# ── 2 · ?run= selection contract (exec-load the page, drive the router) ─────


from nousergon_lib.pipeline_status import (  # noqa: E402
    PipelineExecutionSummary,
    PipelineRun,
    RunStatus,
)

_REGION = "us-east-1"
_ACCOUNT = "711398986525"
_SF = "ne-weekly-freshness-pipeline"
_ARN = f"arn:aws:states:{_REGION}:{_ACCOUNT}:stateMachine:{_SF}"


def _exec_arn(name: str) -> str:
    return f"arn:aws:states:{_REGION}:{_ACCOUNT}:execution:{_SF}:{name}"


def _summary(name: str, role: str | None = None) -> PipelineExecutionSummary:
    return PipelineExecutionSummary(
        execution_arn=_exec_arn(name),
        name=name,
        status=RunStatus.SUCCEEDED,
        start_utc=datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc),
        duration_sec=3600.0,
        pipeline_role=role,
    )


@pytest.fixture
def page_mod():
    """Exec-load the page with streamlit mocked and the loader stubbed so the
    top-level render does no AWS / network I/O."""
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    # Top-level render reads ?run= — default to no param + empty session.
    mock_st.query_params = {}
    mock_st.session_state = {}
    mock_st.button.return_value = False
    mock_st.columns.return_value = [MagicMock() for _ in range(5)]
    sys.modules["streamlit"] = mock_st

    # Stub the loader the page top-level calls so importing/execing it does no
    # network. These are re-bound on the page module after load for the tests.
    from loaders import pipeline_status_loader as psl

    psl.list_recent_pipeline_runs_for_arn = lambda *a, **k: []
    psl.read_pipeline_state_with_fallback = lambda *a, **k: MagicMock(run=None)

    spec = importlib.util.spec_from_file_location("_pipeline_status_under_test", PAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_matching_run_name_returns_execution_arn(page_mod):
    mod = page_mod
    mod.list_recent_pipeline_runs_for_arn = lambda *a, **k: [
        _summary("weekly-2026-07-04"),
        _summary("smoke-abc123", role="smoke"),
    ]
    got = mod._resolve_run_name_to_arn(_ARN, "smoke-abc123")
    assert got == _exec_arn("smoke-abc123")


def test_resolve_matches_trailing_arn_segment(page_mod):
    # Belt-and-suspenders: match on the trailing name segment of the ARN too.
    mod = page_mod
    s = _summary("weekly-2026-07-04")
    mod.list_recent_pipeline_runs_for_arn = lambda *a, **k: [s]
    got = mod._resolve_run_name_to_arn(_ARN, "weekly-2026-07-04")
    assert got == s.execution_arn


def test_resolve_non_matching_run_returns_none(page_mod):
    mod = page_mod
    mod.list_recent_pipeline_runs_for_arn = lambda *a, **k: [
        _summary("weekly-2026-07-04")
    ]
    assert mod._resolve_run_name_to_arn(_ARN, "does-not-exist") is None


def test_resolve_absent_run_returns_none(page_mod):
    mod = page_mod
    mod.list_recent_pipeline_runs_for_arn = lambda *a, **k: [
        _summary("weekly-2026-07-04")
    ]
    assert mod._resolve_run_name_to_arn(_ARN, None) is None
    assert mod._resolve_run_name_to_arn(_ARN, "") is None


def test_resolve_swallows_listing_error(page_mod):
    # A bad deep-link (or a throttled list call) must never break the page.
    mod = page_mod

    def _boom(*a, **k):
        raise RuntimeError("SFN throttled")

    mod.list_recent_pipeline_runs_for_arn = _boom
    assert mod._resolve_run_name_to_arn(_ARN, "anything") is None


# ── _render_section routing: matching ?run= selects it; else falls back ─────


def _capture_render_section(mod, monkeypatch, *, run_param, summaries):
    """Drive _render_section and capture the kwargs the loader was called with,
    so we can assert WHICH execution the router selected."""
    calls = {}

    def _fake_read(arn, *, role_filter=None, execution_arn=None):
        calls["role_filter"] = role_filter
        calls["execution_arn"] = execution_arn
        return MagicMock(run=None)

    monkeypatch.setattr(mod, "list_recent_pipeline_runs_for_arn", lambda *a, **k: summaries)
    monkeypatch.setattr(mod, "read_pipeline_state_with_fallback", _fake_read)
    # Silence the disclosure expander's own list call + rendering.
    monkeypatch.setattr(mod, "_render_recent_executions_disclosure", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_render_run_header", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_render_banner", lambda *a, **k: None)
    mod.st.session_state = {}

    mod._render_section(_ARN, run_param=run_param)
    return calls


def test_render_section_matching_run_selects_that_execution(page_mod, monkeypatch):
    mod = page_mod
    calls = _capture_render_section(
        mod,
        monkeypatch,
        run_param="smoke-abc123",
        summaries=[_summary("weekly-2026-07-04"), _summary("smoke-abc123", role="smoke")],
    )
    # The matching ?run= must pin the section to that execution's ARN and NOT
    # apply the canonical role filter.
    assert calls["execution_arn"] == _exec_arn("smoke-abc123")
    assert calls["role_filter"] is None


def test_render_section_non_matching_run_falls_back_to_canonical(page_mod, monkeypatch):
    mod = page_mod
    calls = _capture_render_section(
        mod,
        monkeypatch,
        run_param="not-a-real-run",
        summaries=[_summary("weekly-2026-07-04")],
    )
    # No match → fall back to the canonical most-recent auto-pick (role_filter
    # for this SF is {"weekly"}), NOT a specific execution.
    assert calls["execution_arn"] is None
    assert calls["role_filter"] == {"weekly"}


def test_render_section_absent_run_falls_back_to_canonical(page_mod, monkeypatch):
    mod = page_mod
    calls = _capture_render_section(
        mod,
        monkeypatch,
        run_param=None,
        summaries=[_summary("weekly-2026-07-04")],
    )
    # Behavior identical to before ?run= existed: canonical role auto-pick.
    assert calls["execution_arn"] is None
    assert calls["role_filter"] == {"weekly"}
