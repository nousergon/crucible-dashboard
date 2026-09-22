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


def test_optimizer_unavailable_renders_error(page_mod):
    """The 2026-09-22 state (alpha-engine-config-I11370).

    The optimizer owned the book and produced no solve. Before schema 1.5.0
    this arrived as `no_rebalance_at_target` and rendered as calm blue
    alongside the words "Valid HOLD, not a fault".
    """
    mod, st = page_mod
    st = _banner_state(mod, st, {
        "book_status": {
            "state": "optimizer_unavailable",
            "headline": "Book HELD — the optimizer produced no usable solve.",
            "optimizer_solved": False,
            "optimizer_failure": "TurnoverBudgetError",
            "turnover_one_way": None, "rebalance_band_pct": None,
            "dispersion": {},
        }
    })
    assert st.error.called
    assert not st.info.called and not st.success.called


def test_an_unknown_state_is_never_rendered_as_calm(page_mod):
    """A producer state this consumer has not been taught yet.

    The old default renderer was ``st.info``, so a new state arrived looking
    healthy — the same shape of failure as the banner it renders.
    """
    mod, st = page_mod
    st = _banner_state(mod, st, {
        "book_status": {"state": "some_future_state",
                        "headline": "something new", "dispersion": {}}
    })
    assert st.warning.called
    assert not st.info.called and not st.success.called


def test_absent_book_status_renders_nothing(page_mod):
    # Pre-1.3.0 artifact → no banner (graceful pre-producer-merge degrade).
    mod, st = page_mod
    st = _banner_state(mod, st, {"summary": {}, "tickers": []})
    assert not st.info.called and not st.error.called
    assert not st.success.called and not st.warning.called


# config#1436 — pricing_source surfaced inline in the decision-chain string.
def test_chain_str_tags_pricing_source(page_mod):
    mod, _ = page_mod
    chain = [
        {"stage": "risk_guard", "result": "pass"},
        {"stage": "position_sizer", "result": "10.00% NAV",
         "pricing_source": "price_history_close"},
        {"stage": "entry_trigger", "result": "pending"},
    ]
    s = mod._chain_str(chain)
    # Fallback price is visible to the operator; live snapshot tagged "live".
    assert "position_sizer:10.00% NAV [last-close]" in s
    live = mod._chain_str([{"stage": "position_sizer", "result": "x",
                            "pricing_source": "ibkr"}])
    assert "[live]" in live
    # Legacy/absent pricing_source → no tag, no crash.
    assert mod._chain_str([{"stage": "position_sizer", "result": "x"}]) \
        == "position_sizer:x"


def _rationale_payload(state):
    """A 2026-09-22-shaped artifact: 1 held name, 2 ENTER signals that got no
    optimizer view and so land in `no_action_unknown` by construction."""
    return {
        "book_status": {
            "state": state,
            "headline": "…",
            "optimizer_solved": state != "optimizer_unavailable",
            "dispersion": {},
        },
        "summary": {"n_considered": 3, "n_held": 1},
        "market_regime": "neutral",
        "signal_date": "2026-09-18",
        "prediction_date": "2026-09-22",
        "run_id": "2609221300",
        "tickers": [
            {"ticker": "ANF", "terminal_state": "held", "held": True},
            {"ticker": "COIN", "terminal_state": "no_action_unknown"},
            {"ticker": "DELL", "terminal_state": "no_action_unknown"},
        ],
    }


def test_unknown_warning_is_suppressed_when_the_optimizer_was_unavailable(page_mod):
    """One fact, told once (alpha-engine-config-I11370).

    With no optimizer view, EVERY ENTER signal lands in `no_action_unknown` —
    `_classify_no_action` returns it whenever `opt_view` is absent. Repeating
    it per-ticker under a banner that already says the optimizer produced
    nothing makes one fault look like two, and the second one looks like a
    per-name anomaly.
    """
    mod, st = page_mod
    st.reset_mock()
    mod._render_rationale(_rationale_payload("optimizer_unavailable"))
    warned = " ".join(str(c) for c in st.warning.call_args_list)
    assert "unknown" not in warned
    assert st.error.called, "the banner itself still renders as an error"


def test_the_unknown_warning_still_fires_when_the_optimizer_DID_run(page_mod):
    """The suppression is scoped to the one state that explains it.

    An unknown no-action state on a day the optimizer solved is a real
    per-ticker anomaly and must keep its warning.
    """
    mod, st = page_mod
    st.reset_mock()
    mod._render_rationale(_rationale_payload("rebalanced"))
    warned = " ".join(str(c) for c in st.warning.call_args_list)
    assert "unknown" in warned
    assert "COIN" in warned and "DELL" in warned
