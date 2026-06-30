"""The schema-1.3.0 book_status banner on the Order Book Rationale page.

Exec-loads ``views/16_Order_Book_Rationale.py`` (with S3 loaders patched to
return nothing, so no network) — which also smoke-tests that the page imports
and execs cleanly — then drives ``_render_book_status_banner`` across the four
states and the pre-1.3.0 (absent-field) fallback, asserting the right
Streamlit alert renderer is chosen.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "16_Order_Book_Rationale.py"


@pytest.fixture
def page_mod():
    sys.path.insert(0, str(REPO_ROOT))
    # Fresh streamlit mock per load so call assertions are isolated.
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    sys.modules["streamlit"] = mock_st

    # Patch the page's S3 loaders to no-ops before exec (no network).
    from loaders import s3_loader
    s3_loader.load_order_book_rationale_history = lambda *a, **k: []
    s3_loader.load_open_orders_latest = lambda *a, **k: None

    spec = importlib.util.spec_from_file_location("_obr_page_under_test", PAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mock_st


def _banner_state(mod, mock_st, payload):
    mock_st.reset_mock()
    mod._render_book_status_banner(payload)
    return mock_st


def test_no_rebalance_renders_info(page_mod):
    mod, st = page_mod
    st = _banner_state(mod, st, {
        "book_status": {
            "state": "no_rebalance_at_target",
            "headline": "No rebalance — already at target.",
            "turnover_one_way": 0.0041, "rebalance_band_pct": 0.25,
            "dispersion": {"n_predictions": 26, "alpha_stdev": 0.0111,
                           "n_up": 24, "n_down": 2, "n_flat": 0},
        }
    })
    assert st.info.called and not st.error.called and not st.success.called


def test_rebalanced_renders_success(page_mod):
    mod, st = page_mod
    st = _banner_state(mod, st, {
        "book_status": {"state": "rebalanced",
                        "headline": "Book rebalanced — 2 entries + 1 exit.",
                        "dispersion": {}}
    })
    assert st.success.called and not st.error.called


def test_hold_book_safeguard_renders_warning(page_mod):
    mod, st = page_mod
    st = _banner_state(mod, st, {
        "book_status": {"state": "hold_book_safeguard",
                        "headline": "Hold-book safeguard fired.",
                        "dispersion": {"signal_degenerate": True,
                                       "alpha_stdev": 0.0008}}
    })
    assert st.warning.called and not st.error.called


def test_allocations_dropped_renders_error(page_mod):
    mod, st = page_mod
    st = _banner_state(mod, st, {
        "book_status": {"state": "allocations_dropped",
                        "headline": "1 allocation dropped.", "dispersion": {}}
    })
    assert st.error.called


def test_absent_book_status_renders_nothing(page_mod):
    # Pre-1.3.0 artifact → no banner (graceful pre-producer-merge degrade).
    mod, st = page_mod
    st = _banner_state(mod, st, {"summary": {}, "tickers": []})
    assert not st.info.called and not st.error.called
    assert not st.success.called and not st.warning.called
