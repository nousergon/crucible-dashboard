"""Regime page fixes from the 2026-07-08 console defect sweep (config#1984).

Item 1 — latent crash: ``f"... {s10:+.2f if s10 else 0.0} ..."`` puts the
conditional inside the format spec, which is invalid — Python tries to parse
``+.2f if s10 else 0.0`` as a format spec and raises, so the
``regime_signal_neutral`` branch crashed at render whenever ``s10`` was
falsy. Fixed by computing the value before the f-string.

Item 2a — retired 10d/30d horizons: the T2 downstream-stratified-Sortino
section headlined ``spread_10d``/``spread_30d``, which config#1456 /
crucible-backtester#428 retired in favor of the canonical 21d primary / 5d
diagnostic horizons (mirrors the same rename ``charts/attribution_chart.py``
documents having already made for the correlation fields).

This is a source-text + isolated-expression guard rather than a full page
exec-load — ``views/15_Regime.py`` is a large page with many S3-backed
module-level loaders; the buggy line itself is a plain f-string expression
that doesn't depend on any of that, so pinning it directly is both cheaper
and more precise than fixture-building a full render.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
REGIME_SRC = (REPO_ROOT / "views" / "15_Regime.py").read_text()


class TestFormatSpecCrashFixed:
    def test_conditional_no_longer_inside_format_spec(self):
        # The exact broken pattern from I1984 item 1 must not reappear.
        assert ":+.2f if s10 else 0.0" not in REGIME_SRC

    def test_neutral_branch_spread_computed_before_fstring(self):
        assert "_neutral_spread = s_primary if s_primary else 0.0" in REGIME_SRC

    def test_old_broken_expression_actually_raised(self):
        # Reproduces the exact latent crash the issue described, to prove
        # this test would have caught the regression: an f-string cannot
        # embed a conditional expression inside the format-spec portion
        # (after the ':'); Python tries to parse "+.2f if s10 else 0.0" as
        # a format spec literal and raises TypeError/ValueError.
        s10 = None
        raised = False
        try:
            f"{s10:+.2f if s10 else 0.0}"
        except (TypeError, ValueError):
            raised = True
        assert raised, "expected the old pattern to raise on a falsy value"

    def test_new_pattern_does_not_raise_on_falsy_spread(self):
        s10 = None
        spread = s10 if s10 else 0.0
        # Must not raise.
        rendered = f"{spread:+.2f}"
        assert rendered == "+0.00"

    def test_new_pattern_does_not_raise_on_zero_spread(self):
        s10 = 0.0
        spread = s10 if s10 else 0.0
        assert f"{spread:+.2f}" == "+0.00"

    def test_new_pattern_preserves_real_values(self):
        s10 = 0.42
        spread = s10 if s10 else 0.0
        assert f"{spread:+.2f}" == "+0.42"


class TestRetiredHorizonsRemoved:
    def test_spread_10d_30d_keys_gone_from_render_logic(self):
        # Only the explanatory comment may mention the retired names (to
        # document the rename); the actual dict-key lookups must be gone.
        assert 't2_latest.get("spread_10d"' not in REGIME_SRC
        assert 't2_latest.get("spread_30d"' not in REGIME_SRC
        assert 'entry.get("spread_10d")' not in REGIME_SRC
        assert 'entry.get("spread_30d")' not in REGIME_SRC

    def test_canonical_horizon_source_imported(self):
        assert "from loaders.outcome_store import PRIMARY_HORIZON_DAYS" in REGIME_SRC
        assert "diagnostic_horizons" in REGIME_SRC
        assert "from nousergon_lib.quant.horizons import DEFAULT_POLICY" in REGIME_SRC

    def test_headline_keys_are_horizon_derived(self):
        assert '_primary_key = f"spread_{PRIMARY_HORIZON_DAYS}d"' in REGIME_SRC
        assert '_diag_key = f"spread_{_T2_DIAGNOSTIC_HORIZON_DAYS}d"' in REGIME_SRC


class TestSignalLifecycleCaptionFixed:
    def test_stale_10_30_day_caption_gone(self):
        src = (REPO_ROOT / "views" / "11_Signal_Lifecycle.py").read_text()
        assert "10-day and 30-day windows" not in src

    def test_caption_uses_primary_horizon(self):
        src = (REPO_ROOT / "views" / "11_Signal_Lifecycle.py").read_text()
        assert "PRIMARY_HORIZON_DAYS" in src
